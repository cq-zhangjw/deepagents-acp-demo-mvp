"""Web Assistant MCP Server: HTTP requests, webpage fetching, Baidu search, link extraction.

Provides async tools for the DeepAgents web assistant:
- http_request : send arbitrary HTTP requests (method / url / headers / body)
- fetch_webpage: GET a URL and return the raw HTML source
- baidu_search : search Baidu and return top-N titles / snippets / urls
- extract_links: fetch a page and list all <a> hyperlinks

All tools catch their own errors and return the error text instead of raising,
so a failing tool does not break the agent loop (same pattern as calc_server).
"""

import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Web Assistant Server")

# Only http/https targets are allowed (no file://, data: ...).
_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_TIMEOUT = 15.0
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _check_url(url: str) -> None:
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"only http/https URLs are allowed, got scheme {scheme!r}")


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate text at max_bytes (UTF-8), appending a notice when cut."""
    if not text or max_bytes <= 0:
        return text
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes].decode("utf-8", errors="replace")
    return f"{cut}\n\n[... output truncated at {max_bytes} bytes ...]"


def _clean_html(raw: str) -> str:
    """Strip script/style/tags and collapse whitespace."""
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


async def _client(timeout: float) -> httpx.AsyncClient:
    # verify=False keeps internal / self-signed sites reachable; this is a
    # local AI tool with the user's explicit authorization to fetch targets.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={"User-Agent": _DEFAULT_UA},
        verify=False,
    )


@mcp.tool()
async def http_request(
    method: str,
    url: str,
    headers: dict | None = None,
    body: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = 100000,
) -> str:
    """Send an arbitrary HTTP request and return the status, key headers and (truncated) body.

    Args:
        method: HTTP method: GET / POST / PUT / PATCH / DELETE / HEAD
        url: target URL (http/https only)
        headers: optional extra headers as a dict, e.g. {"Accept-Language": "zh-CN"}
        body: optional request body string (for POST/PUT/PATCH)
        timeout: request timeout in seconds
        max_bytes: max response body bytes to return (<=0 means unlimited)

    Returns:
        JSON string with ok, status_code, content_type, final_url and body text (truncated).
    """
    try:
        _check_url(url)
        method = (method or "GET").upper()
        async with await _client(timeout) as client:
            resp = await client.request(method, url, headers=headers, content=body)
            text = resp.text or ""
            return json.dumps(
                {
                    "ok": True,
                    "status_code": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "final_url": str(resp.url),
                    "body": _truncate(text, max_bytes),
                },
                ensure_ascii=False,
            )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@mcp.tool()
async def fetch_webpage(url: str, max_bytes: int = 20000, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Fetch a URL via GET and return the raw HTML source (truncated).

    Args:
        url: target URL (http/https only)
        max_bytes: max returned bytes of the raw HTML source (<=0 unlimited)
        timeout: request timeout in seconds

    Returns:
        The page source text; includes a truncation notice when the page is
        larger than max_bytes.
    """
    try:
        _check_url(url)
        async with await _client(timeout) as client:
            resp = await client.get(url)
            return _truncate(resp.text or "", max_bytes)
    except Exception as e:  # noqa: BLE001
        return f"Error fetching {url}: {type(e).__name__}: {e}"


@mcp.tool()
async def baidu_search(
    keyword: str,
    num: int = 5,
    timeout: float = _DEFAULT_TIMEOUT,
    cookies: str | None = None,
) -> str:
    """Search Baidu for a keyword and return the top-N results.

    Args:
        keyword: the search keyword / query string
        num: how many results to return (1-20)
        timeout: request timeout in seconds
        cookies: optional Cookie header string copied from a browser session,
            e.g. "BAIDUID=xxx; BDUSS=yyy". Baidu blocks cookie-less datacenter
            requests with a captcha page; passing browser cookies bypasses it.

    Returns:
        JSON string: {"ok": true, "keyword": ..., "results": [{"title", "snippet", "url"}]}
        When Baidu responds with its anti-bot captcha page, returns
        {"ok": false, "error": ..., "hint": ...} explaining the block.
    """
    try:
        num = max(1, min(int(num), 20))
        headers = {"Cookie": cookies} if cookies else None
        async with await _client(timeout) as client:
            resp = await client.get(
                "https://www.baidu.com/s", params={"wd": keyword, "rn": num}, headers=headers
            )
            resp.encoding = resp.charset_encoding or "utf-8"
            html = resp.text or ""
        if "安全验证" in html or "captcha" in html[:1000].lower():
            return json.dumps(
                {
                    "ok": False,
                    "error": "Baidu returned its anti-bot captcha page (no browser cookies)",
                    "hint": (
                        "Pass browser cookies via the cookies parameter, or fetch another "
                        "search engine directly, e.g. https://www.bing.com/search?q=<query> "
                        "with http_request / fetch_webpage."
                    ),
                },
                ensure_ascii=False,
            )
        results = _parse_baidu(html, num)
        if not results:
            return json.dumps(
                {"ok": False, "error": "No results parsed (page structure changed or empty)"},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "keyword": keyword, "results": results}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def _parse_baidu(html: str, num: int) -> list[dict]:
    """Parse Baidu SERP: h3 titles + result urls + abstract snippets, kept in order."""
    h3_re = re.compile(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>', re.S | re.I)
    titles = []
    for href, title_html in h3_re.findall(html):
        title = _clean_html(title_html)
        if title:
            titles.append((title, href))
    abs_re = re.compile(
        r'<span[^>]*class="[^"]*(?:c-abstract|content-right)[^"]*"[^>]*>(.*?)</span>',
        re.S | re.I,
    )
    abstracts = [x for x in (_clean_html(a) for a in abs_re.findall(html)) if x]
    results = []
    for i, (title, href) in enumerate(titles[:num]):
        snippet = abstracts[i] if i < len(abstracts) else ""
        results.append({"title": title, "snippet": snippet, "url": href})
    return results


@mcp.tool()
async def extract_links(url: str, max_bytes: int = 20000, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Fetch a page and extract all hyperlinks (<a href>).

    Args:
        url: target URL (http/https only)
        max_bytes: max fetched bytes of the page source (<=0 unlimited)
        timeout: request timeout in seconds

    Returns:
        JSON string: {"ok": true, "links": [{"text", "href"}]}
    """
    try:
        _check_url(url)
        async with await _client(timeout) as client:
            resp = await client.get(url)
            html = resp.text or ""
        html = html[:max_bytes] if max_bytes > 0 else html
        a_re = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
        links = []
        for href, text_html in a_re.findall(html):
            href = href.strip()
            if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
                continue
            text = _clean_html(text_html)[:200]
            links.append({"text": text, "href": urljoin(url, href)})
        return json.dumps({"ok": True, "links": links[:200]}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


if __name__ == "__main__":
    import sys

    sys.stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    mcp.run(transport="stdio")
