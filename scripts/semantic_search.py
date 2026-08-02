#!/usr/bin/env python3
"""
MoiraiCore — Semantic Vault Search (Feature 6)

Adds meaning-based search on top of the keyword FTS5 index. Two backends:

  1. "tfidf"  (DEFAULT, zero deps, fully offline)
     Builds a TF-IDF representation of every vault document in pure stdlib
     (math + collections + json) and ranks by cosine similarity to the query.
     Catches paraphrase / synonym / topical matches that FTS5 keyword search
     misses (e.g. "how do I log in" -> files about "authentication").

  2. "ollama" (optional, behind SEMANTIC_BACKEND=ollama)
     Uses a local Ollama embedding model (default nomic-embed-text) via the
     stdlib urllib HTTP client. No third-party packages required. If Ollama is
     unreachable or errors, it degrades transparently back to tfidf.

The public entry point is `semantic_search(query, docs, top_k, backend=None)`
where `docs` is a list of {"id","name","folder","content","path",...}. It
returns the same dict shape as VaultIndex.search so the UI renders uniformly:

    {"id","path","name","folder","score","snippet","size"}

Embedding vectors for the ollama backend are cached on disk (JSON, keyed by
document id + content hash) so repeated queries are cheap and offline after
the first build.
"""

import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── Configuration ──────────────────────────────────────────────────────────
SEMANTIC_BACKEND = os.environ.get("SEMANTIC_BACKEND", "tfidf").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Cache directory for ollama vectors: <AGENT_OS_ROOT>/memory-vault/.semantic
_AGENT_OS_ROOT = os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1]))
_EMBED_CACHE_DIR = os.path.join(_AGENT_OS_ROOT, "memory-vault", ".semantic")

# Tokenisation: lowercase words of length >= 2, drop pure punctuation.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]+")


def _tokenize(text):
    return _TOKEN_RE.findall((text or "").lower())


def _norm(vec):
    s = math.sqrt(sum(v * v for v in vec.values()))
    return s or 1.0


def _cosine(a, b):
    # a, b are sparse vectors (dicts) already L2-normalised.
    if len(a) <= len(b):
        small, big = a, b
    else:
        small, big = b, a
    dot = sum(w * big.get(k, 0.0) for k, w in small.items())
    return dot  # both normalised => dot == cosine


# ── TF-IDF backend ──────────────────────────────────────────────────────────
def _build_tfidf(docs):
    """Return (doc_vectors, idf) where doc_vectors[id] is a normalised dict."""
    df = Counter()
    tokenised = {}
    for d in docs:
        toks = _tokenize(d.get("content", ""))
        # also fold in name/folder for topical signal
        toks += _tokenize(d.get("name", "")) * 2
        toks += _tokenize(d.get("folder", "")) * 2
        uniq = set(toks)
        tokenised[d["id"]] = Counter(toks)
        for t in uniq:
            df[t] += 1

    n = max(len(docs), 1)
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    doc_vectors = {}
    for d in docs:
        tf = tokenised[d["id"]]
        vec = {}
        for t, c in tf.items():
            vec[t] = (1.0 + math.log(c)) * idf[t]
        doc_vectors[d["id"]] = {k: v / _norm(vec) for k, v in vec.items()} if vec else {}
    return doc_vectors


def _tfidf_search(query, docs, top_k):
    if not docs:
        return []
    doc_vectors = _build_tfidf(docs)
    q_toks = _tokenize(query)
    if not q_toks:
        return []
    q_vec = {}
    qc = Counter(q_toks)
    for t, c in qc.items():
        q_vec[t] = 1.0 + math.log(c)
    q_norm = _norm(q_vec)
    q_vec = {k: v / q_norm for k, v in q_vec.items()}

    scored = []
    for d in docs:
        vec = doc_vectors.get(d["id"])
        if not vec:
            continue
        sim = _cosine(q_vec, vec)
        if sim > 0:
            scored.append((sim, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [_to_result(d, sim, query) for sim, d in scored[:top_k]]


# ── Ollama embedding backend ─────────────────────────────────────────────────
def _embed_ollama(text):
    """Call Ollama /api/embed and return a 1-D float list. Raises on any error."""
    payload = json.dumps({"model": OLLAMA_EMBED_MODEL, "input": text}).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # Ollama returns {"embeddings": [[...]]} for input string, or list-of-lists.
    emb = data.get("embeddings")
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        return emb[0]
    if isinstance(emb, list):
        return emb
    raise ValueError("unexpected ollama embed response")


def _content_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _load_embed_cache():
    os.makedirs(_EMBED_CACHE_DIR, exist_ok=True)
    cache = {}
    for fn in os.listdir(_EMBED_CACHE_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(_EMBED_CACHE_DIR, fn)) as fh:
                    rec = json.load(fh)
                cache[rec["id"]] = rec
            except Exception:
                continue
    return cache


def _save_embed(rec):
    os.makedirs(_EMBED_CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]", "_", rec["id"].lower())[:120]
    with open(os.path.join(_EMBED_CACHE_DIR, f"{safe}.json"), "w") as fh:
        json.dump(rec, fh)


def _ollama_search(query, docs, top_k):
    """Embed via Ollama; fall back to TF-IDF on any failure."""
    try:
        cache = _load_embed_cache()
        vectors = {}
        for d in docs:
            h = _content_hash(d.get("content", ""))
            rec = cache.get(d["id"])
            if rec and rec.get("hash") == h and rec.get("vec"):
                vec = rec["vec"]
            else:
                vec = _embed_ollama(d.get("content", ""))
                _save_embed({"id": d["id"], "hash": h, "vec": vec})
            vectors[d["id"]] = vec

        q_vec = _embed_ollama(query)

        def cossim(a, b):
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(x * x for x in b)) or 1.0
            # dot product of normalised vectors
            # vectors are dense; use simple dot over min length
            m = min(len(a), len(b))
            dot = sum(a[i] * b[i] for i in range(m))
            return dot / (na * nb)

        scored = []
        for d in docs:
            vec = vectors.get(d["id"])
            if not vec:
                continue
            sim = cossim(q_vec, vec)
            if sim > 0:
                scored.append((sim, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [_to_result(d, sim, query) for sim, d in scored[:top_k]]
    except (URLError, HTTPError, ValueError, OSError, TimeoutError) as e:
        # Transparent degradation to the offline backend.
        return _tfidf_search(query, docs, top_k)


# ── Shared result shaping ────────────────────────────────────────────────────
def _to_result(doc, score, query):
    content = doc.get("content", "") or ""
    q = query.lower().strip()
    idx = content.lower().find(q)
    if idx >= 0:
        snippet = content[max(0, idx - 60): idx + len(q) + 60].replace("\n", " ").strip()
    else:
        snippet = content[:120].replace("\n", " ").strip()
    return {
        "id": doc["id"],
        "path": doc.get("path", doc["id"]),
        "name": doc.get("name", doc["id"]).replace("-", " ").replace("_", " ").title(),
        "folder": doc.get("folder", ""),
        "score": round(score, 4),
        "snippet": snippet,
        "size": doc.get("size", len(content)),
        "semantic": True,
    }


def semantic_search(query, docs, top_k=10, backend=None):
    """
    Public entry point.

    query  : natural-language query string
    docs   : list of {"id","name","folder","content","path","size"}
    top_k  : max results
    backend: "tfidf" | "ollama" | None (use SEMANTIC_BACKEND env, default tfidf)

    Returns a list of result dicts shaped like VaultIndex.search().
    """
    if not query or not query.strip():
        return []
    backend = (backend or SEMANTIC_BACKEND).lower()
    if backend == "ollama":
        return _ollama_search(query, docs, top_k)
    return _tfidf_search(query, docs, top_k)


# ── CLI smoke test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sample = [
        {"id": "notes/auth.md", "name": "auth", "folder": "notes",
         "content": "How to authenticate and log in to the dashboard using a bearer token.",
         "path": "notes/auth.md", "size": 80},
        {"id": "notes/sleep.md", "name": "sleep", "folder": "notes",
         "content": "The agent should pause and wait before retrying a failed network call.",
         "path": "notes/sleep.md", "size": 80},
        {"id": "projects/horde.md", "name": "horde", "folder": "projects",
         "content": "Multi-agent orchestration: spawn worker agents to parallelise coding tasks.",
         "path": "projects/horde.md", "size": 80},
    ]
    q = sys.argv[1] if len(sys.argv) > 1 else "sign in with token"
    for r in semantic_search(q, sample, top_k=3):
        print(f"{r['score']:.3f}  {r['name']:10}  {r['snippet']}")
