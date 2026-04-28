"""MCP server exposing RAG search over the indexed vault.

Serves the modern streamable-http transport on /mcp/ — the single endpoint
that current MCP clients (Hermes, Claude Code, etc.) speak by default.
The legacy SSE transport (separate /sse + /messages/ endpoints) is no
longer wired up; nothing in this cluster consumes it.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chromadb
import requests
from mcp.server.fastmcp import FastMCP
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rag-mcp")

CHROMADB_URL = os.environ.get("CHROMADB_URL", "http://chromadb:8000")
# llama.cpp serves the OpenAI-compatible API under /v1. LLAMA_URL is the
# base server address (no trailing /v1); /v1/embeddings is appended below.
LLAMA_URL = os.environ.get("LLAMA_URL", "http://llama-embed.llama-cpp.svc.cluster.local:8080")
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
    # OpenAI-style response: {data: [{index, embedding}, ...]}.
    resp = requests.post(f"{LLAMA_URL}/v1/embeddings", json={
        "model": EMBED_MODEL,
        "input": [text],
    }, timeout=60)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


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
    log.info(f"llama.cpp: {LLAMA_URL}")
    log.info(f"Embed model: {EMBED_MODEL}")
    log.info(f"Vault: {VAULT_PATH}")

    # Streamable-HTTP transport. FastMCP exposes the protocol on a single
    # /mcp/ endpoint that handles both initialize/list-tools/call-tool
    # requests AND server-pushed notifications, all over POST. Clients
    # connect with `url: http://host:port/mcp/`.
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8080
    mcp.run(transport="streamable-http")
