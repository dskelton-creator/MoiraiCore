#!/usr/bin/env python3
"""
MoiraiCore — Infinite Context Engine
====================================
Automatically injects relevant vault context into every agent session.

How it works:
  1. On startup, scans the vault and builds a relevance index (TF-IDF + tags + links)
  2. When a task/query comes in, scores all vault content by relevance
  3. Selects the top-N most relevant context chunks within the token budget
  4. Prepends them to the agent's prompt as structured context blocks
  5. Tracks what was injected per session to avoid repetition

Usage:
    from infinite_context import InfiniteContext
    engine = InfiniteContext()
    engine.build_index()                          # scan vault, build relevance index
    context = engine.get_context("research ASX market trends", max_tokens=2000)
    # Returns: {"chunks": [...], "total_tokens": 450, "sources": [...]}

Integration:
    Called automatically by hermes_bridge.py cmd_ask() before every session.
    No manual invocation needed after initial setup.
"""

import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
VAULT = AGENT_OS_ROOT / "memory-vault"
WORKSPACE = AGENT_OS_ROOT / "workspace"
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
INDEX_CACHE = AGENT_OS_ROOT / "config" / "context-engine.json"

# Ensure vault_index is importable
sys.path.insert(0, str(SCRIPTS_DIR))


# ─── Token estimation ───

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ─── Text processing ───

def tokenize(text: str) -> list[str]:
    """Simple tokenizer: lowercase, split on non-alpha, filter stopwords."""
    STOPWORDS = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "both",
        "each", "few", "more", "most", "other", "some", "such", "no", "nor",
        "not", "only", "own", "same", "so", "than", "too", "very", "just",
        "because", "but", "and", "or", "if", "while", "about", "up", "it",
        "its", "this", "that", "these", "those", "i", "me", "my", "we", "our",
        "you", "your", "he", "him", "his", "she", "her", "they", "them",
        "their", "what", "which", "who", "whom", "also", "get", "got",
    }
    words = re.findall(r"[a-z][a-z0-9_\-]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def extract_tags(content: str) -> list[str]:
    """Extract #tags and [[wikilinks]] from markdown content."""
    tags = set()
    # #tags
    for m in re.findall(r"(?<!`)#([A-Za-z][A-Za-z0-9_\-]{2,})\b", content):
        tags.add(m.lower())
    # [[wikilinks]]
    for m in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", content):
        tags.add(m.strip().lower().replace(" ", "-"))
    return list(tags)


def extract_headings(content: str) -> list[str]:
    """Extract markdown headings as topic signals."""
    headings = []
    for m in re.findall(r"^#{1,4}\s+(.+)$", content, re.MULTILINE):
        headings.append(m.strip().lower())
    return headings


def chunk_content(content: str, max_chunk_tokens: int = 500) -> list[str]:
    """
    Split content into semantic chunks at paragraph/heading boundaries.
    Each chunk stays within max_chunk_tokens.
    """
    if not content:
        return []

    # Split on double newlines (paragraphs) or headings
    paragraphs = re.split(r"\n(?=#{1,4}\s)|\n{2,}", content)
    chunks = []
    current = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_tokens = estimate_tokens(para)

        if current_tokens + para_tokens > max_chunk_tokens and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_tokens = para_tokens
        else:
            current.append(para)
            current_tokens += para_tokens

    if current:
        chunks.append("\n\n".join(current))

    return chunks


# ─── TF-IDF Index ───

class TfIdfIndex:
    """Simple in-memory TF-IDF index for vault content."""

    def __init__(self):
        self.doc_freqs = Counter()      # term → number of docs containing it
        self.term_freqs = {}            # doc_id → Counter(term → count)
        self.doc_lengths = {}           # doc_id → token count
        self.total_docs = 0

    def add_doc(self, doc_id: str, tokens: list[str]):
        """Index a document."""
        tf = Counter(tokens)
        self.term_freqs[doc_id] = tf
        self.doc_lengths[doc_id] = len(tokens)
        for term in tf:
            self.doc_freqs[term] += 1
        self.total_docs += 1

    def score(self, doc_id: str, query_tokens: list[str]) -> float:
        """Score a document against a query using TF-IDF cosine similarity."""
        if doc_id not in self.term_freqs or self.total_docs == 0:
            return 0.0

        tf = self.term_freqs[doc_id]
        doc_len = self.doc_lengths[doc_id]
        if doc_len == 0:
            return 0.0

        score = 0.0
        for term in query_tokens:
            if term in tf and term in self.doc_freqs:
                # TF: normalized term frequency
                term_tf = tf[term] / doc_len
                # IDF: inverse document frequency with smoothing
                idf = math.log((self.total_docs + 1) / (self.doc_freqs[term] + 1)) + 1
                score += term_tf * idf

        return score


    def score_from_tokens(self, chunk_tokens: list[str], query_tokens: list[str]) -> float:
        """Score a chunk's tokens against query tokens (used for intra-file chunk selection)."""
        if not chunk_tokens or not query_tokens:
            return 0.0
        overlap = len(set(chunk_tokens) & set(query_tokens))
        return overlap / len(query_tokens)


# ─── Main Engine ───

class InfiniteContext:
    """
    Infinite Context Engine — auto-injects relevant vault context into agent sessions.

    Usage:
        engine = InfiniteContext()
        engine.build_index()

        # Get context for a task
        result = engine.get_context("research ASX market trends", max_tokens=2000)
        # result = {"chunks": [...], "total_tokens": 450, "sources": [...], "tags_matched": [...]}

        # Get context for a specific agent
        result = engine.get_context("write a blog post", agent_key="writer", max_tokens=1500)
    """

    def __init__(self, vault_path=None, max_context_tokens=3000):
        self.vault = Path(vault_path) if vault_path else VAULT
        self.max_context_tokens = max_context_tokens
        self.index = TfIdfIndex()
        self.files = {}          # doc_id → {path, content, tags, headings, folder, mtime, hash}
        self.tag_index = {}      # tag → [doc_id, ...]
        self.link_graph = {}     # doc_id → [linked_doc_id, ...]
        self.injected = {}       # session_id → [doc_id, ...]  (track what was already injected)
        self._built = False

    # ─── Index building ───

    def build_index(self, force=False):
        """
        Scan vault, build TF-IDF index, tag index, and link graph.
        Uses cache if available and recent (< 5 min old) unless force=True.
        """
        # Check cache
        if not force and INDEX_CACHE.exists():
            try:
                cache = json.loads(INDEX_CACHE.read_text())
                cache_age = time.time() - cache.get("built_at", 0)
                if cache_age < 300:  # 5 min cache
                    self._load_cache(cache)
                    self._built = True
                    return {"ok": True, "cached": True, "files": len(self.files)}
            except Exception:
                pass

        # Scan vault
        if not self.vault.exists():
            return {"ok": False, "error": f"Vault not found: {self.vault}"}

        self.index = TfIdfIndex()
        self.files = {}
        self.tag_index = {}
        self.link_graph = {}

        file_count = 0
        for f in self.vault.rglob("*.md"):
            if f.name.startswith("."):
                continue
            try:
                rel = str(f.relative_to(self.vault))
                content = f.read_text(errors="ignore")
                stat = f.stat()
                file_hash = hashlib.md5(content.encode()).hexdigest()[:16]

                tokens = tokenize(content)
                tags = extract_tags(content)
                headings = extract_headings(content)

                self.files[rel] = {
                    "path": rel,
                    "content": content,
                    "tags": tags,
                    "headings": headings,
                    "folder": f.parent.name.lower(),
                    "mtime": stat.st_mtime,
                    "hash": file_hash,
                    "name": f.stem.replace("-", " ").replace("_", " ").title(),
                }

                # Index
                self.index.add_doc(rel, tokens)

                # Tag index
                for tag in tags:
                    self.tag_index.setdefault(tag, []).append(rel)

                # Link graph (wikilinks)
                wikilinks = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", content)
                self.link_graph[rel] = [
                    link.strip().lower().replace(" ", "-")
                    for link in wikilinks
                ]

                file_count += 1
            except Exception:
                continue

        self._built = True

        # Write cache
        try:
            cache_data = {
                "built_at": time.time(),
                "file_count": file_count,
                "files": {k: {kk: vv for kk, vv in v.items() if kk != "content"}
                          for k, v in self.files.items()},
                "tag_index": self.tag_index,
                "link_graph": self.link_graph,
            }
            INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            INDEX_CACHE.write_text(json.dumps(cache_data))
        except Exception:
            pass

        return {"ok": True, "cached": False, "files": file_count}

    def _load_cache(self, cache):
        """Load index from cache (content re-read from disk)."""
        self.tag_index = cache.get("tag_index", {})
        self.link_graph = cache.get("link_graph", {})

        # Re-read content from disk
        for rel, meta in cache.get("files", {}).items():
            fpath = self.vault / rel
            if fpath.exists():
                content = fpath.read_text(errors="ignore")
                tokens = tokenize(content)
                self.files[rel] = {
                    "path": rel,
                    "content": content,
                    "tags": meta.get("tags", []),
                    "headings": meta.get("headings", []),
                    "folder": meta.get("folder", ""),
                    "mtime": meta.get("mtime", 0),
                    "hash": meta.get("hash", ""),
                    "name": meta.get("name", ""),
                }
                self.index.add_doc(rel, tokens)

    # ─── Context retrieval ───

    def get_context(self, query: str, agent_key: str | None = None,
                    max_tokens: int | None = None, session_id: str | None = None) -> dict:
        """
        Get relevant context for a query/task.

        Returns:
            {
                "chunks": [{"text": "...", "source": "...", "score": 0.85, "folder": "..."}],
                "total_tokens": 450,
                "sources": ["path1", "path2"],
                "tags_matched": ["seo", "asx"],
                "agent_memory_included": True,
            }
        """
        if not self._built:
            self.build_index()

        max_tokens = max_tokens or self.max_context_tokens
        query_tokens = tokenize(query)
        query_tags = set(extract_tags(query))

        if not query_tokens:
            return {"chunks": [], "total_tokens": 0, "sources": [], "tags_matched": []}

        # ── Score all files ──
        scores = {}  # doc_id → score

        for doc_id in self.files:
            score = 0.0

            # 1. TF-IDF relevance
            tfidf_score = self.index.score(doc_id, query_tokens)
            score += tfidf_score * 3.0  # Weight TF-IDF heavily

            # 2. Tag overlap
            file_tags = set(self.files[doc_id].get("tags", []))
            tag_overlap = len(query_tags & file_tags)
            score += tag_overlap * 2.0

            # 3. Heading match
            headings = self.files[doc_id].get("headings", [])
            heading_text = " ".join(headings)
            heading_tokens = tokenize(heading_text)
            heading_overlap = len(set(query_tokens) & set(heading_tokens))
            score += heading_overlap * 1.5

            # 4. Folder relevance boost
            folder = self.files[doc_id].get("folder", "")
            folder_boosts = {
                "context": 1.2,
                "agents": 1.5,
                "projects": 1.3,
                "research": 1.4,
                "daily": 0.8,
                "skills": 1.1,
                "loop": 0.7,
            }
            score *= folder_boosts.get(folder, 1.0)

            # 5. Recency boost (files modified in last 7 days get a boost)
            mtime = self.files[doc_id].get("mtime", 0)
            age_days = (time.time() - mtime) / 86400
            if age_days < 7:
                score *= 1.3
            elif age_days < 30:
                score *= 1.1

            # 6. Link graph proximity (files linked to already-scored files)
            # This creates a "neighborhood" effect — related content bubbles up
            links = self.link_graph.get(doc_id, [])
            for link in links:
                for other_id in self.files:
                    if link in other_id.lower() and other_id in scores:
                        score += scores[other_id] * 0.1  # Small boost from linked files

            # 7. Agent-specific boost
            if agent_key:
                agent_folder = f"agents/{agent_key}"
                if folder == "agents" and agent_key in doc_id.lower():
                    score *= 2.0  # Strong boost for agent's own memory

            # Penalize already-injected content for this session
            if session_id and session_id in self.injected:
                if doc_id in self.injected[session_id]:
                    score *= 0.1  # Heavy penalty for already-injected

            if score > 0:
                scores[doc_id] = score

        # ── Select top files by score ──
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # ── Build context chunks within token budget ──
        chunks = []
        total_tokens = 0
        sources = []
        tags_matched = set()
        agent_memory_included = False

        for doc_id, score in ranked:
            if total_tokens >= max_tokens:
                break

            f = self.files[doc_id]
            content = f["content"]

            # For agent memory files, include the full content (it's usually small)
            if f["folder"] == "agents" and agent_key and agent_key in doc_id.lower():
                chunk_text = content
                agent_memory_included = True
            else:
                # For other files, extract the most relevant chunks
                file_chunks = chunk_content(content, max_chunk_tokens=400)
                # Score each chunk individually and take the best
                chunk_scores = []
                for chunk in file_chunks:
                    chunk_tokens = tokenize(chunk)
                    chunk_score = self.index.score_from_tokens(chunk_tokens, query_tokens)
                    chunk_scores.append((chunk, chunk_score))
                chunk_scores.sort(key=lambda x: x[1], reverse=True)
                chunk_text = chunk_scores[0][0] if chunk_scores else ""

            if not chunk_text:
                continue

            chunk_tokens = estimate_tokens(chunk_text)
            if total_tokens + chunk_tokens > max_tokens:
                # Truncate to fit budget
                remaining = max_tokens - total_tokens
                if remaining < 50:
                    break
                chunk_text = chunk_text[:remaining * 4]
                chunk_tokens = estimate_tokens(chunk_text)

            chunks.append({
                "text": chunk_text,
                "source": doc_id,
                "name": f["name"],
                "folder": f["folder"],
                "score": round(score, 3),
            })
            sources.append(doc_id)
            total_tokens += chunk_tokens
            tags_matched.update(set(f.get("tags", [])) & query_tags)

        # Track what was injected
        if session_id:
            self.injected[session_id] = sources

        return {
            "chunks": chunks,
            "total_tokens": total_tokens,
            "sources": sources,
            "tags_matched": list(tags_matched),
            "agent_memory_included": agent_memory_included,
        }

    def format_context_block(self, context: dict) -> str:
        """
        Format context dict into a structured prompt block for agent injection.
        """
        if not context["chunks"]:
            return ""

        lines = ["# 📚 Auto-Injected Context", ""]
        lines.append(f"*Injected {context['total_tokens']} tokens from {len(context['sources'])} sources*")
        lines.append("")

        for chunk in context["chunks"]:
            lines.append(f"## {chunk['name']} (`{chunk['source']}`)")
            lines.append("")
            lines.append(chunk["text"])
            lines.append("")

        return "\n".join(lines)

    def get_stats(self) -> dict:
        """Get engine statistics."""
        return {
            "files_indexed": len(self.files),
            "total_terms": len(self.index.doc_freqs),
            "tags": len(self.tag_index),
            "links": sum(len(v) for v in self.link_graph.values()),
            "sessions_tracked": len(self.injected),
            "built": self._built,
        }


# ─── CLI ───

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Infinite Context Engine")
    sub = parser.add_subparsers(dest="command")

    # build
    p_build = sub.add_parser("build", description="Build/rebuild the context index")
    p_build.add_argument("--force", action="store_true", help="Force rebuild even if cache is fresh")

    # query
    p_query = sub.add_parser("query", help="Get context for a query")
    p_query.add_argument("query", help="The query/task")
    p_query.add_argument("--agent", default=None, help="Agent key for agent-specific boost")
    p_query.add_argument("--tokens", type=int, default=2000, help="Max context tokens")
    p_query.add_argument("--raw", action="store_true", help="Output raw context text only")

    # stats
    p_stats = sub.add_parser("stats", help="Show engine statistics")

    args = parser.parse_args()

    engine = InfiniteContext()

    if args.command == "build":
        result = engine.build_index(force=args.force)
        print(json.dumps(result, indent=2))

    elif args.command == "query":
        engine.build_index()
        context = engine.get_context(args.query, agent_key=args.agent, max_tokens=args.tokens)
        if args.raw:
            print(engine.format_context_block(context))
        else:
            print(json.dumps(context, indent=2))

    elif args.command == "stats":
        engine.build_index()
        print(json.dumps(engine.get_stats(), indent=2))

    else:
        parser.print_help()
