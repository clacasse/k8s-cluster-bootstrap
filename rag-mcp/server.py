"""MCP server exposing RAG search over the indexed vault."""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chromadb
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rag-mcp")

CHROMADB_URL = os.environ.get("CHROMADB_URL", "http://chromadb:8000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.ollama.svc.cluster.local:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "vault")
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault")).resolve()

mcp = FastMCP("vault-search")

_lock = threading.Lock()
_client = None
_collection = None


def get_collection():
    global _client, _collection
    with _lock:
        try:
            if _collection is not None:
                _collection.count()
                return _collection
        except Exception:
            _client = None
            _collection = None

        parsed = urlparse(CHROMADB_URL)
        _client = chromadb.HttpClient(
            host=parsed.hostname,
            port=parsed.port or 8000,
        )
        _collection = _client.get_or_create_collection(name=COLLECTION_NAME)
        return _collection


def embed_query(text: str) -> list[float]:
    resp = requests.post(f"{OLLAMA_URL}/api/embed", json={
        "model": EMBED_MODEL,
        "input": [text],
    }, timeout=60)
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


@mcp.tool()
def search_notes(query: str, limit: int = 5) -> str:
    """Search your vault for notes semantically related to the query.

    Returns the most relevant chunks from your notes, ranked by relevance.
    Each result includes the file path, heading, and content excerpt.

    Args:
        query: What to search for (natural language).
        limit: Maximum number of results (default 5).
    """
    try:
        collection = get_collection()
        count = collection.count()
        if count == 0:
            return "No results found — the vault index is empty."

        query_embedding = embed_query(query)
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(limit, 20, count),
        )
    except Exception as e:
        log.error(f"Search failed: {e}")
        return f"Search failed: {e}"

    if not results["documents"][0]:
        return "No results found."

    output = []
    for i, (doc, meta, distance) in enumerate(zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    )):
        heading = f" > {meta['heading']}" if meta.get("heading") else ""
        relevance = max(0, round((1 - distance / 2) * 100, 1))
        output.append(
            f"**{meta['file_path']}{heading}** ({relevance}% relevant)\n\n{doc}\n"
        )

    return "\n---\n".join(output)


@mcp.tool()
def list_recent_notes(days: int = 7) -> str:
    """List files in the vault that were modified recently.

    Args:
        days: How many days back to look (default 7).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []

    for path in VAULT_PATH.rglob("*"):
        if not path.is_file():
            continue
        if any(p in str(path) for p in [".obsidian", ".git", ".trash", "node_modules"]):
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime >= cutoff:
            rel = str(path.relative_to(VAULT_PATH))
            size = path.stat().st_size
            recent.append((rel, mtime, size))

    recent.sort(key=lambda x: x[1], reverse=True)

    if not recent:
        return f"No files modified in the last {days} days."

    lines = [f"Files modified in the last {days} days:\n"]
    for rel, mtime, size in recent:
        age = datetime.now(timezone.utc) - mtime
        if age.days > 0:
            ago = f"{age.days}d ago"
        elif age.seconds > 3600:
            ago = f"{age.seconds // 3600}h ago"
        else:
            ago = f"{age.seconds // 60}m ago"
        lines.append(f"- **{rel}** ({ago}, {size:,} bytes)")

    return "\n".join(lines)


@mcp.tool()
def read_note(path: str) -> str:
    """Read the full content of a specific note from the vault.

    Args:
        path: Relative path to the file within the vault (e.g. "projects/README.md").
    """
    full_path = (VAULT_PATH / path).resolve()

    # Security: check path traversal before any filesystem access
    try:
        full_path.relative_to(VAULT_PATH)
    except ValueError:
        return f"Access denied: {path}"

    if not full_path.exists():
        return f"File not found: {path}"
    if not full_path.is_file():
        return f"Not a file: {path}"

    try:
        content = full_path.read_text(errors="replace")
    except Exception as e:
        return f"Could not read {path}: {e}"

    return f"# {path}\n\n{content}"


if __name__ == "__main__":
    log.info(f"ChromaDB: {CHROMADB_URL}")
    log.info(f"Ollama: {OLLAMA_URL}")
    log.info(f"Embed model: {EMBED_MODEL}")
    log.info(f"Vault: {VAULT_PATH}")

    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    sse = SseServerTransport("/messages/", security_settings=security)

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        ) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream, write_stream, mcp._mcp_server.create_initialization_options()
            )
        return Response()

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)
