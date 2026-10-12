"""MCP server exposing MooFile knowledge base (database) CRUD over the REST API.

This FastMCP server wraps the MooFile backend HTTP API
(see api/routers/databases.py and api/routers/documents.py) and exposes the
following MCP tools:

- create_knowledge_base(name, db_type)   -> create a knowledge base
- get_knowledge_base(db_id)              -> fetch knowledge base details by id
- list_knowledge_bases()                 -> list all non-trashed knowledge bases
- rename_knowledge_base(db_id, new_name) -> rename a knowledge base
- delete_knowledge_base(db_id)           -> soft-delete a knowledge base
- get_db_id_by_name(db_name)             -> resolve db_id from an exact name
- upload_and_vectorize_document(local_path, db_id, ...)
    -> upload a local document into a vector knowledge base and vectorize it
- list_documents(db_id)                  -> list documents of a knowledge base
- delete_document(db_id, doc_id)         -> delete a document (and its chunks)
- retrieve_knowledge(db_id, query, ...)  -> semantic search over a vector knowledge base

All calls go through the `requests` library. The server runs over the
streamable-http MCP transport, so it can be reached at
http://<host>:<port>/mcp.

Configuration (environment variables):
- MOOFILE_API_BASE   base URL of the MooFile backend, default http://127.0.0.1:8888
- MOOFILE_API_TIMEOUT  request timeout in seconds, default 10
- MOOFILE_MCP_HOST    host to bind the MCP server, default 127.0.0.1
- MOOFILE_MCP_PORT    port to bind the MCP server, default 8010
- MOOFILE_MCP_TRANSPORT  MCP transport: "streamable-http" (default) or "stdio"
"""
import mimetypes
import os
import time

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:  # pragma: no cover - dotenv is optional
    pass

import requests
from fastmcp import FastMCP

API_BASE = os.environ.get("MOOFILE_API_BASE", "http://127.0.0.1:8888")
API_TIMEOUT = float(os.environ.get("MOOFILE_API_TIMEOUT", "10"))

MCP_HOST = os.environ.get("MOOFILE_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MOOFILE_MCP_PORT", "8010"))
MCP_TRANSPORT = os.environ.get("MOOFILE_MCP_TRANSPORT", "streamable-http")

# The MooFile API wraps every response in {code, data, message}; code 0 means success.
OK_CODE = 0

# Extensions the backend vectorization worker can actually parse into text.
# (see task_service._load_text); other extensions are rejected by the backend
# worker even though the upload endpoint would accept them.
VECTORIZABLE_EXTS = {".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".json", ".log"}

# Polling interval (seconds) and default deadline when waiting for a task.
TASK_POLL_INTERVAL = 2.0
DEFAULT_TASK_TIMEOUT = 600.0

mcp = FastMCP("MooFile Knowledge Server")


def _request(method: str, path: str, **kwargs) -> object:
    """Send a request to the MooFile API and return the unwrapped `data`.

    Raises RuntimeError with a descriptive message on transport failure,
    HTTP errors, non-zero business codes, or malformed responses.
    """
    url = f"{API_BASE}{path}"
    kwargs.setdefault("timeout", API_TIMEOUT)
    try:
        response = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to reach MooFile API at {url}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"MooFile API returned non-JSON response (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        ) from exc

    if response.status_code >= 400 or payload.get("code") != OK_CODE:
        raise RuntimeError(
            f"MooFile API error (HTTP {response.status_code}, code {payload.get('code')}): "
            f"{payload.get('message')}"
        )
    return payload.get("data")


@mcp.tool()
def create_knowledge_base(name: str, db_type: str = "normal") -> dict:
    """Create a new knowledge base.

    Args:
        name: Knowledge base name. Must be 2-64 characters of letters,
            digits or underscores, and unique across all knowledge bases.
        db_type: Database type, either "normal" (plain JSON records) or
            "vector" (vector knowledge base with embeddings). Defaults to "normal".

    Returns:
        The created knowledge base details, including its generated db_id.
    """
    return _request("POST", "/api/databases", json={"name": name, "type": db_type})


@mcp.tool()
def get_knowledge_base(db_id: str) -> dict:
    """Fetch the details of a knowledge base by its db_id.

    Args:
        db_id: The knowledge base id (e.g. "db_MY_CSDN_de49cda0").

    Returns:
        Knowledge base details: id, name, type, recordCount, documentCount,
        fieldCount, sizeMB, createdAt, updatedAt, trashed, and vectorConfig
        for vector knowledge bases.
    """
    return _request("GET", f"/api/databases/{db_id}")


@mcp.tool()
def list_knowledge_bases() -> list[dict]:
    """List all non-trashed knowledge bases.

    Returns:
        A list of knowledge base summaries with id, name, type and counts.
    """
    return _request("GET", "/api/databases")


@mcp.tool()
def rename_knowledge_base(db_id: str, new_name: str) -> dict:
    """Rename an existing knowledge base.

    Args:
        db_id: The id of the knowledge base to rename.
        new_name: The new name. Must be 2-64 characters of letters, digits or
            underscores, and unique across all knowledge bases.

    Returns:
        The updated knowledge base details. Note that renaming may change the
        db_id (the id is derived from the name), so the returned object is the
        authoritative source for the new id.
    """
    return _request("PUT", f"/api/databases/{db_id}", json={"name": new_name})


@mcp.tool()
def delete_knowledge_base(db_id: str) -> dict:
    """Soft-delete a knowledge base by moving it to the trash.

    The knowledge base is not physically removed and can still be restored
    through the backend trash endpoints.

    Args:
        db_id: The id of the knowledge base to delete.

    Returns:
        A confirmation payload: {"id": db_id, "trashed": True}.
    """
    return _request("DELETE", f"/api/databases/{db_id}")


@mcp.tool()
def get_db_id_by_name(db_name: str) -> str:
    """Resolve the db_id of a knowledge base from its exact name.

    Args:
        db_name: The exact knowledge base name (case-insensitive match).

    Returns:
        The matching db_id (e.g. "db_MY_CSDN_de49cda0").

    Raises:
        RuntimeError: If no knowledge base with the given name exists.
    """
    databases = _request("GET", "/api/databases")
    for database in databases:
        if str(database.get("name", "")).casefold() == db_name.casefold():
            return database["id"]
    raise RuntimeError(f"No knowledge base found with name: {db_name}")


def _find_document(db_id: str, doc_id: str) -> dict:
    """Locate a document record by id inside a knowledge base."""
    documents = _request("GET", f"/api/databases/{db_id}/documents")
    for document in documents:
        if document.get("id") == doc_id or document.get("_id") == doc_id:
            return document
    raise RuntimeError(f"No document found with id: {doc_id}")


def _wait_for_task(db_id: str, task_id: str, timeout: float) -> dict:
    """Poll the backend task list until the given task reaches a terminal state.

    Returns the terminal task record, or the last observed record if the
    timeout expires first (the returned record then still carries the current
    status so callers can report it accurately).
    """
    deadline = time.monotonic() + max(timeout, 1.0)
    last_seen: dict = {}
    while True:
        tasks = _request(
            "GET", "/api/tasks",
            params={"dbId": db_id, "type": "vectorization"},
        )
        for task in tasks:
            if task.get("id") == task_id or task_id in task.get("docIds", []):
                last_seen = task
                if task.get("status") in ("completed", "failed", "cancelled"):
                    return task
        if time.monotonic() >= deadline:
            return last_seen or {"id": task_id, "status": "unknown"}
        time.sleep(TASK_POLL_INTERVAL)


@mcp.tool()
def upload_and_vectorize_document(
    local_path: str,
    db_id: str,
    chunk_size: int = 500,
    overlap: int = 20,
    model: str = "",
    wait: bool = True,
    timeout: float = DEFAULT_TASK_TIMEOUT,
) -> dict:
    """Upload a local document into a vector knowledge base and vectorize it.

    The file is read from disk and sent to the MooFile upload endpoint, then
    a vectorization task is started for it. When `wait` is True the tool polls
    the task until it finishes and reports the final state and chunk count.

    Args:
        local_path: Absolute path of the document file on this machine.
            Supported extensions (parseable by the backend worker): .txt, .md,
            .markdown, .csv, .html, .htm, .json, .log.
        db_id: The target knowledge base id; it must be a vector knowledge base.
        chunk_size: Target chunk size in characters (100-2000, default 500).
        overlap: Chunk overlap in characters (0 <= overlap < chunk_size, default 20).
        model: Embedding model name; leave empty to use the knowledge base default.
        wait: If True (default), block until the vectorization task completes.
        timeout: Max seconds to wait for the task (default 600).

    Returns:
        A summary with the uploaded document info, the vectorization task
        status, and the final chunk count (when available).
    """
    if not os.path.isfile(local_path):
        raise RuntimeError(f"Local file does not exist: {local_path}")

    ext = os.path.splitext(local_path)[1].lower()
    if ext not in VECTORIZABLE_EXTS:
        raise RuntimeError(
            f"Unsupported file type '{ext}': the backend vectorization worker "
            f"can only parse {sorted(VECTORIZABLE_EXTS)}"
        )

    filename = os.path.basename(local_path)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(local_path, "rb") as file_obj:
        doc = _request(
            "POST",
            f"/api/databases/{db_id}/documents/upload",
            files={"file": (filename, file_obj, mime_type)},
        )
    doc_id = doc.get("id") or doc.get("_id")

    tasks = _request(
        "POST",
        f"/api/databases/{db_id}/documents/vectorize",
        json={
            "docIds": [doc_id],
            "chunkSize": chunk_size,
            "overlap": overlap,
            "model": model or None,
        },
    )
    task_id = (tasks[0] or {}).get("id") if tasks else None

    result = {
        "document": doc,
        "dbId": db_id,
        "vectorizeStarted": bool(tasks),
    }
    if wait:
        task = _wait_for_task(db_id, task_id or "", timeout)
        result["taskStatus"] = task.get("status")
        if task.get("status") == "completed":
            try:
                fresh_doc = _find_document(db_id, doc_id)
                result["chunkCount"] = fresh_doc.get("chunkCount", 0)
                result["docStatus"] = fresh_doc.get("docStatus")
            except RuntimeError:
                result["chunkCount"] = 0
        else:
            result["taskStatusDetail"] = task.get("target", "") or ""
    return result


@mcp.tool()
def list_documents(db_id: str) -> list[dict]:
    """List all documents of a knowledge base.

    Args:
        db_id: The knowledge base id.

    Returns:
        A list of document records with id, name, type, size, uploadTime,
        docStatus, vectorStatus and chunkCount. The document id can be passed
        to delete_document.
    """
    return _request("GET", f"/api/databases/{db_id}/documents")


@mcp.tool()
def delete_document(db_id: str, doc_id: str) -> dict:
    """Delete a document (and all of its vectorized chunks) from a knowledge base.

    Args:
        db_id: The knowledge base id.
        doc_id: The document id to delete (see list_documents).

    Returns:
        A confirmation payload: {"deleted": 1}.
    """
    return _request(
        "POST",
        f"/api/databases/{db_id}/documents/delete",
        json={"ids": [doc_id]},
    )


@mcp.tool()
def retrieve_knowledge(
    db_id: str,
    query: str,
    top_k: int = 0,
    threshold: float = -1.0,
) -> list[dict]:
    """Semantic search over a vector knowledge base (RAG retrieval).

    The query is embedded with the knowledge base's configured model and the
    most similar chunks are returned. Omitting top_k / threshold makes the
    backend fall back to the knowledge base's vectorConfig defaults.

    Args:
        db_id: The vector knowledge base id.
        query: The natural-language query text.
        top_k: Max number of results (use 0 to fall back to the database
            default, normally 10).
        threshold: Minimum similarity score to keep a result, 0-1
            (use -1 to fall back to the database default, normally 0.3).

    Returns:
        A list of hits, each with rank, score, source (document name),
        chunkIndex, content and documentId, ordered by similarity descending.
    """
    body: dict = {"query": query}
    if top_k > 0:
        body["topK"] = top_k
    if threshold >= 0:
        body["threshold"] = threshold
    return _request(
        "POST",
        f"/api/databases/{db_id}/retrieval",
        json=body,
    )


if __name__ == "__main__":
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport="streamable-http",
            host=MCP_HOST,
            port=MCP_PORT,
            show_banner=False,
        )
