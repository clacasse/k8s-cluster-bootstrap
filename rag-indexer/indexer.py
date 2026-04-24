"""RAG indexer: watches a vault directory, chunks files, embeds via the
OpenAI-compatible llama.cpp server, stores in ChromaDB."""

import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rag-indexer")

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault"))
CHROMADB_URL = os.environ.get("CHROMADB_URL", "http://chromadb:8000")
# llama.cpp serves the OpenAI-compatible API under /v1. LLAMA_URL is the
# base server address (no trailing /v1); we append /v1/embeddings below.
LLAMA_URL = os.environ.get("LLAMA_URL", "http://llama-embed.llama-cpp.svc.cluster.local:8080")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "vault")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
EXCLUDE_PATTERNS = os.environ.get("EXCLUDE_PATTERNS", ".obsidian,node_modules,.git,.trash").split(",")
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "2000"))

SUPPORTED_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".cfg", ".ini", ".toml"}


def should_index(path: Path) -> bool:
    for pattern in EXCLUDE_PATTERNS:
        if pattern.strip() in str(path):
            return False
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def chunk_markdown(text: str, file_path: str) -> list[dict]:
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    chunks = []
    positions = [(m.start(), m.group(1), m.group(2)) for m in heading_pattern.finditer(text)]

    if not positions:
        for i in range(0, len(text), CHUNK_SIZE):
            chunk_text = text[i:i + CHUNK_SIZE].strip()
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "heading": "",
                    "file_path": file_path,
                    "chunk_index": len(chunks),
                })
        if not chunks:
            fallback = text.strip()
            if fallback:
                chunks.append({"text": fallback, "heading": "", "file_path": file_path, "chunk_index": 0})
        return chunks

    for i, (start, level, heading) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        section_text = text[start:end].strip()

        if len(section_text) <= CHUNK_SIZE:
            if section_text:
                chunks.append({
                    "text": section_text,
                    "heading": heading,
                    "file_path": file_path,
                    "chunk_index": len(chunks),
                })
        else:
            for j in range(0, len(section_text), CHUNK_SIZE):
                sub = section_text[j:j + CHUNK_SIZE].strip()
                if sub:
                    chunks.append({
                        "text": sub,
                        "heading": heading,
                        "file_path": file_path,
                        "chunk_index": len(chunks),
                    })

    return chunks


def chunk_text(text: str, file_path: str) -> list[dict]:
    if file_path.endswith(".md"):
        return chunk_markdown(text, file_path)
    chunks = []
    for i in range(0, len(text), CHUNK_SIZE):
        chunk_text = text[i:i + CHUNK_SIZE].strip()
        if chunk_text:
            chunks.append({
                "text": chunk_text,
                "heading": "",
                "file_path": file_path,
                "chunk_index": len(chunks),
            })
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    # OpenAI-style payload: {model, input: [str, ...]}. llama.cpp returns
    # {data: [{index, embedding: [float, ...]}, ...], model, usage, object}.
    # Preserve input order by sorting on `index`; llama-server has returned
    # them in order in practice, but the spec doesn't guarantee it.
    resp = requests.post(f"{LLAMA_URL}/v1/embeddings", json={
        "model": EMBED_MODEL,
        "input": texts,
    }, timeout=120)
    resp.raise_for_status()
    rows = sorted(resp.json()["data"], key=lambda r: r["index"])
    return [row["embedding"] for row in rows]


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def chunk_id(file_path: str, chunk_index: int) -> str:
    return hashlib.md5(f"{file_path}:{chunk_index}".encode()).hexdigest()


def scan_vault() -> dict[str, float]:
    files = {}
    for path in VAULT_PATH.rglob("*"):
        if path.is_file() and should_index(path):
            rel = str(path.relative_to(VAULT_PATH))
            files[rel] = path.stat().st_mtime
    return files


def index_file(collection, rel_path: str):
    full_path = VAULT_PATH / rel_path
    try:
        text = full_path.read_text(errors="replace")
    except Exception as e:
        log.warning(f"Could not read {rel_path}: {e}")
        return

    if not text.strip():
        return

    chunks = chunk_text(text, rel_path)
    if not chunks:
        return

    texts = [c["text"] for c in chunks]
    try:
        embeddings = embed(texts)
    except Exception as e:
        log.error(f"Embedding failed for {rel_path}: {e}")
        return

    # Remove old chunks first to avoid orphans when chunk count decreases
    remove_file(collection, rel_path)

    ids = [chunk_id(rel_path, c["chunk_index"]) for c in chunks]
    metadatas = [{"file_path": c["file_path"], "heading": c["heading"], "chunk_index": c["chunk_index"]} for c in chunks]

    collection.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    log.info(f"Indexed {rel_path} ({len(chunks)} chunks)")


def remove_file(collection, rel_path: str):
    results = collection.get(where={"file_path": rel_path})
    if results["ids"]:
        collection.delete(ids=results["ids"])
        log.info(f"Removed {rel_path} ({len(results['ids'])} chunks)")


def run():
    log.info(f"Vault: {VAULT_PATH}")
    log.info(f"ChromaDB: {CHROMADB_URL}")
    log.info(f"llama.cpp: {LLAMA_URL}")
    log.info(f"Embed model: {EMBED_MODEL}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")

    # Wait for dependencies. llama.cpp's server exposes a /health endpoint
    # that returns 200 once the model is loaded — that's stricter than
    # /v1/models (which can 200 while weights are still memory-mapping).
    for name, url, path in [("ChromaDB", CHROMADB_URL, "/api/v2/heartbeat"), ("llama.cpp", LLAMA_URL, "/health")]:
        for attempt in range(60):
            try:
                resp = requests.get(f"{url}{path}", timeout=5)
                resp.raise_for_status()
                log.info(f"{name} is ready")
                break
            except Exception:
                if attempt % 10 == 0:
                    log.info(f"Waiting for {name}...")
                time.sleep(5)
        else:
            log.error(f"{name} not reachable at {url}")
            sys.exit(1)

    parsed = urlparse(CHROMADB_URL)
    client = chromadb.HttpClient(
        host=parsed.hostname,
        port=parsed.port or 8000,
    )
    collection = client.get_or_create_collection(name=COLLECTION_NAME)

    # Track file state
    known_files: dict[str, float] = {}

    log.info("Starting initial index...")
    current_files = scan_vault()
    for rel_path in current_files:
        index_file(collection, rel_path)
        known_files[rel_path] = current_files[rel_path]
    log.info(f"Initial index complete: {len(current_files)} files")

    # Watch loop
    log.info("Watching for changes...")
    while True:
        time.sleep(POLL_INTERVAL)
        current_files = scan_vault()

        # New or modified files
        for rel_path, mtime in current_files.items():
            if rel_path not in known_files or known_files[rel_path] < mtime:
                index_file(collection, rel_path)
                known_files[rel_path] = mtime

        # Deleted files
        for rel_path in list(known_files.keys()):
            if rel_path not in current_files:
                remove_file(collection, rel_path)
                del known_files[rel_path]


if __name__ == "__main__":
    run()
