"""MCP server exposing RAG search over the indexed vault."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chromadb
import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rag-mcp")

CHROMADB_URL = os.environ.get("CHROMADB_URL", "http://chromadb:8000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.ollama.svc.cluster.local:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "vault")
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))

mcp = FastMCP("vault-search")

_client = None
_collection = None


def get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.HttpClient(host=CHROMADB_URL)
        _collection = _client.get_collection(name=COLLECTION_NAME)
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
    collection = get_collection()
    query_embedding = embed_query(query)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(limit, 20),
    )

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
    full_path = VAULT_PATH / path
    if not full_path.exists():
        return f"File not found: {path}"
    if not full_path.is_file():
        return f"Not a file: {path}"

    # Security: ensure the path doesn't escape the vault
    try:
        full_path.resolve().relative_to(VAULT_PATH.resolve())
    except ValueError:
        return f"Access denied: {path}"

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
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream, write_stream, mcp._mcp_server.create_initialization_options()
            )

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)
