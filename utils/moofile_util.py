
import os
import uuid
from moofile import Collection

# NOTE: vector_util (sentence-transformers) is imported lazily inside the
# vector methods below, so plain-table usage never requires that heavy stack.

class MooFileUtil:
    """Manage regular and vector-enabled MooFile collections."""

    def __init__(self, root_dir: str, is_vector: bool = False, embedding_model = None):
        """Initialize the utility with a storage root and optional vector model."""
        self.root_dir = root_dir
        self.is_vector = is_vector
        self.embedding_model = embedding_model

    def create_database(self, db_name: str):
        """Create an empty BSON collection and return its file path."""
        os.makedirs(self.root_dir, exist_ok=True)
        db_path = self.get_database_path(db_name)
        if os.path.exists(db_path):
            raise FileExistsError(f"Database '{db_name}' already exists.")
        with Collection(db_path):
            pass
        return db_path

    def delete_database(self, db_name: str):
        """Delete a collection file and its metadata file when present."""
        db_path = self.get_database_path(db_name)
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"Database '{db_name}' does not exist.")
        os.remove(db_path)
        meta_path = f"{db_path}.meta"
        if os.path.exists(meta_path):
            os.remove(meta_path)

    def list_databases(self):
        """Return the sorted names of all BSON collections in the root directory."""
        if not os.path.isdir(self.root_dir):
            return []
        return sorted(
            os.path.splitext(name)[0]
            for name in os.listdir(self.root_dir)
            if name.endswith(".bson")
            and os.path.isfile(os.path.join(self.root_dir, name))
        )


    def database_exists(self, db_name: str):
        """Return whether the named collection file exists."""
        return os.path.isfile(self.get_database_path(db_name))

    def get_database_path(self, db_name: str):
        """Build the BSON file path for a collection name."""
        filename = db_name if db_name.endswith(".bson") else f"{db_name}.bson"
        return os.path.join(self.root_dir, filename)


    def rename_database(self, old_name: str, new_name: str):
        """Rename a collection and its metadata file, returning the new path."""
        old_path = self.get_database_path(old_name)
        new_path = self.get_database_path(new_name)
        if not os.path.exists(old_path):
            raise FileNotFoundError(f"Database '{old_name}' does not exist.")
        if os.path.exists(new_path):
            raise FileExistsError(f"Database '{new_name}' already exists.")
        os.rename(old_path, new_path)
        if os.path.exists(f"{old_path}.meta"):
            os.rename(f"{old_path}.meta", f"{new_path}.meta")
        return new_path

    def copy_database(self, source_name: str, target_name: str):
        """Copy a collection and its metadata file to a new collection name."""
        source_path = self.get_database_path(source_name)
        target_path = self.get_database_path(target_name)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Database '{source_name}' does not exist.")
        if os.path.exists(target_path):
            raise FileExistsError(f"Database '{target_name}' already exists.")
        import shutil
        shutil.copy2(source_path, target_path)
        if os.path.exists(f"{source_path}.meta"):
            shutil.copy2(f"{source_path}.meta", f"{target_path}.meta")
        return target_path

    def valid_database(self, db_name: str):
        """Raise FileNotFoundError when the named collection does not exist."""
        if not self.database_exists(db_name):
            raise FileNotFoundError(f"Database '{db_name}' does not exist.")

    def insert_data(self, db_name: str, data: list[dict], indexes: list):
        """Insert multiple records into a collection using the supplied indexes."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        vector_indexes=None
        if self.is_vector:
            vector_indexes = {"embedding": 384}

        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.insert_many(data)

    def query_data(self, db_name: str, query: dict, indexes: list):
        """Return all records matching a MooFile query."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not query:
            query = {}

        vector_indexes=None
        if self.is_vector:
            vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            return db.find(query).to_list()

    def delete_data(self, db_name: str, query: dict, indexes: list):
        """Delete all records matching a MooFile query."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not query:
            query = {}
        vector_indexes=None
        if self.is_vector:
            vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.delete_many(query)

    def update_data(self, db_name: str, query: dict, update: dict, indexes: list):
        """Apply field updates to every record matching a query."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not query:
            query = {}
        vector_indexes=None
        if self.is_vector:
            vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.update_many(query, set=update)

    def count_data(self, db_name: str, query: dict, indexes: list):
        """Count records matching a MooFile query."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not query:
            query = {}

        vector_indexes=None
        if self.is_vector:
            vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            return db.count(query)

    def vector_search(self, db_name: str, text: str, indexes: list, top_k: int = 5):
        """Return the top semantic matches for text from a vector collection."""
        if not self.is_vector:
            raise ValueError("Vector search is only supported when is_vector is True.")
        from utils.vector_util import encode_text
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        query = {"embedding": encode_text(text, self.embedding_model)}  # Assuming encode_text is imported from vector_util.py

        vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            return db.find({}).vector_search(
                "embedding", query["embedding"], limit=top_k
            ).to_list()

    def generate_id(self, prefix: str = ""):
        """Generate a short UUID-based identifier with an optional prefix."""
        return f"id_{prefix}_{str(uuid.uuid4())[0:8]}"


    def get_document_id(self, source: str, db_name: str = "", table_name: str = "document_meta"):
        """Return the persistent document ID associated with a source within a knowledge base.

        The document identity is scoped to the composite key ``(db_name, source)``.
        This fixes the earlier bug where ``document_meta`` only keyed on ``source``:
        the same file name uploaded into two different knowledge bases previously
        shared one document ID, and deleting a document from one base could wipe the
        metadata of another. Now each knowledge base owns its own document records.
        """
        scope = {"source": source, "db_name": db_name}
        indexes = ["source", "db_name"]

        if not self.database_exists(table_name):
            self.create_database(table_name)
            id = self.generate_id("doc")
            self.insert_data(table_name, [{"id": id, **scope}], indexes)
            return id
        result = self.query_data(table_name, scope, indexes)
        if result:
            return result[0]["id"]
        else:
            id = self.generate_id("doc")
            self.insert_data(table_name, [{"id": id, **scope}], indexes)
            return id

    def insert_document_data(self, db_name: str, doc_list: list, indexes: list):
        """Add IDs and embeddings to document chunks, then insert them."""
        if not self.is_vector:
            raise ValueError("Document insertion with vector embeddings is only supported when is_vector is True.")
        from utils.vector_util import encode_text
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        for index, doc in enumerate(doc_list):
            if self.is_vector:
                doc["document_id"] = self.get_document_id(doc["metadata"]["source"], db_name)
                doc["chunk_id"] = self.generate_id(f"chunk_{index + 1}")
                doc["embedding"] = encode_text(doc["page_content"], self.embedding_model)

        vector_indexes = {"embedding": 384}

        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.insert_many(doc_list)

    def update_document_data(self, db_name: str, query: dict, page_content: str, indexes: list):
        """Update matching document text and regenerate its embedding."""
        if not self.is_vector:
            raise ValueError("Document update with vector embeddings is only supported when is_vector is True.")
        from utils.vector_util import encode_text
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not query:
            query = {}

        if self.is_vector:
            update = {"page_content": page_content, "embedding": encode_text(page_content, self.embedding_model)}

        vector_indexes = {"embedding": 384}
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.update_many(query, set=update)


    def delete_document_data(self, db_name: str, document_id: str, indexes: list):
        """Delete all chunks and metadata associated with a document ID."""
        self.valid_database(db_name)
        db_path = self.get_database_path(db_name)

        if not document_id:
            raise ValueError("document_id must be provided.")

        query = {"document_id": document_id}

        vector_indexes = {"embedding": 384} if self.is_vector else None
        with Collection(db_path, indexes=indexes, vector_indexes=vector_indexes) as db:
            db.delete_many(query)

        # Only remove the meta record that belongs to THIS knowledge base, so a
        # document with the same source in another base is left untouched.
        query = {"id": document_id, "db_name": db_name}
        self.delete_data("document_meta", query, ["id", "db_name"])