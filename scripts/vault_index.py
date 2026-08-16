#!/usr/bin/env python3
"""
MoiraiCore — SQLite Vault Index v2
Fast indexed search and knowledge graph for the memory vault.

Enhanced link extraction:
- [[wikilinks]] — explicit markdown links
- #tags — inline tags
- folder — files in same folder are connected
- agent-output — agents linked to their output files
- topic-similarity — files sharing significant keywords
- agent-role — agent profiles linked to their memory/readme/docs
- subfolder — parent folder ↔ subfolder containment

Schema:
- files: id, path, name, folder, content, size, mtime, hash
- files_fts: FTS5 virtual table for full-text search
- links: source_id, target_id, link_type
- meta, agent_activity, agent_outputs

Usage:
    from vault_index import VaultIndex
    idx = VaultIndex()
    idx.sync()                         # scan vault and update index
    idx.rebuild_graph()                # full re-index with enhanced links
    results = idx.search("security")   # full-text search
    graph = idx.graph()                # knowledge graph data
"""

import hashlib
import json
import os
import re
import sqlite3
import string
from collections import Counter
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1]))) / "memory-vault"
WORKSPACE = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1]))) / "workspace"
DB_PATH = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1]))) / "config" / "vault-index.db"

# ── Stop words for topic similarity ──────────────────────────────────
_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "are", "was",
    "be", "as", "has", "have", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "not", "no", "if", "then",
    "than", "so", "such", "when", "where", "which", "who", "whom", "what",
    "how", "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "any", "its", "his", "her", "their", "our", "your", "my",
    "about", "into", "through", "during", "before", "after", "above",
    "below", "between", "out", "off", "over", "under", "again", "further",
    "once", "here", "there", "also", "just", "only", "very", "too",
    "new", "old", "first", "last", "long", "great", "little", "right",
    "still", "find", "use", "make", "like", "well", "way", "even", "much",
    "back", "now", "get", "got", "come", "go", "see", "know", "take",
    "think", "say", "said", "one", "two", "three", "up", "down",
    "agent", "file", "folder", "notes", "note", "profile", "review",
    "audit", "analysis", "research", "content", "data", "info",
}

# ── Agent output patterns: agent name -> path prefixes ───────────────
_AGENT_OUTPUT_PATTERNS = {
    "seo": ["agents/seo/reports/", "agents/seo/findings/"],
    "researcher": ["agents/researcher/findings/", "research/"],
    "writer": ["agents/writer/drafts/", "agents/writer/published/"],
    "developer": ["agents/developer/decisions/", "agents/developer/snippets/"],
    "threat": ["agents/threat/reports/", "agents/threat/findings/"],
    "hermes": ["agents/hermes/outputs/"],
}


class VaultIndex:
    """SQLite-backed vault index with FTS5 full-text search + enhanced graph."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        self._ensure_db()

    def _ensure_db(self):
        """Create database and tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                folder TEXT NOT NULL,
                content TEXT DEFAULT '',
                size INTEGER DEFAULT 0,
                mtime REAL DEFAULT 0,
                hash TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
            CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
            CREATE INDEX IF NOT EXISTS idx_files_id ON files(id);

            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                content,
                name,
                path,
                folder,
                content='files',
                content_rowid='rowid',
                tokenize='porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                FOREIGN KEY (source_id) REFERENCES files(id),
                FOREIGN KEY (target_id) REFERENCES files(id)
            );

            CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_id);
            CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_id);
            CREATE INDEX IF NOT EXISTS idx_links_type ON links(link_type);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS agent_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                task TEXT DEFAULT '',
                status TEXT DEFAULT 'completed',
                duration_ms INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                details TEXT DEFAULT '',
                timestamp TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_activity_agent ON agent_activity(agent);
            CREATE INDEX IF NOT EXISTS idx_activity_status ON agent_activity(status);
            CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON agent_activity(timestamp);

            CREATE TABLE IF NOT EXISTS agent_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                task_id TEXT DEFAULT '',
                output_path TEXT DEFAULT '',
                output_type TEXT DEFAULT 'text',
                quality_score INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_outputs_agent ON agent_outputs(agent);

            CREATE TABLE IF NOT EXISTS entity_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_ref TEXT NOT NULL,
                state TEXT DEFAULT '{}',
                task_id TEXT DEFAULT '',
                goal_id TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_entity_states_ref ON entity_states(entity_ref);
            CREATE INDEX IF NOT EXISTS idx_entity_states_time ON entity_states(updated_at);
            """
        )
        # Keep FTS5 index in sync
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                INSERT INTO files_fts(rowid, content, name, path, folder)
                VALUES (new.rowid, new.content, new.name, new.path, new.folder);
            END;

            CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, content, name, path, folder)
                VALUES ('delete', old.rowid, old.content, old.name, old.path, old.folder);
            END;

            CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, content, name, path, folder)
                VALUES ('delete', old.rowid, old.content, old.name, old.path, old.folder);
                INSERT INTO files_fts(rowid, content, name, path, folder)
                VALUES (new.rowid, new.content, new.name, new.path, new.folder);
            END;
            """
        )
        conn.commit()
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _file_hash(self, path):
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return ""

    # ── Link Extraction v2 ────────────────────────────────────────────

    def _tokenize(self, text):
        """Extract meaningful lowercase tokens from text."""
        text = text.lower()
        # Remove markdown formatting
        text = re.sub(r'[#*\-\[\](){}|`_>~]', ' ', text)
        tokens = re.findall(r'[a-z][a-z0-9_\-]{2,}', text)
        return [t for t in tokens if t not in _STOP_WORDS and len(t) > 2]

    def _extract_keywords(self, text, top_n=15):
        """Extract top keywords by frequency."""
        tokens = self._tokenize(text)
        freq = Counter(tokens)
        return {word for word, _ in freq.most_common(top_n)}

    def _extract_links(self, content, source_path):
        """
        Enhanced link extraction — combines all link types:
        - wikilinks: [[target]]
        - tags: #tag
        - folder: same-folder co-membership
        - agent-output: agent profile → its output files
        - topic-similarity: shared keywords between files
        - agent-role: agent profile → its memory/readme/docs
        - subfolder: parent ↔ child folder containment
        """
        links = []
        source_path = source_path.lower() if source_path else source_path

        # 1. Wikilinks [[target]]
        for m in re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", content):
            target = m.strip().lower()
            if target and target != source_path:
                links.append({"source": source_path, "target": target, "type": "wikilink", "weight": 3.0})

        # 2. Tags #tag
        for tag in set(re.findall(r"(?<!`)#([A-Za-z][A-Za-z0-9_-]{2,})\b", content)):
            target = f"#{tag.lower()}"
            links.append({"source": source_path, "target": target, "type": "tag", "weight": 2.0})

        return links

    def _add_folder_links(self, all_files):
        """Connect files in the same folder (clique, max 8 per folder to avoid noise)."""
        links = {}
        folder_groups = {}
        for f in all_files:
            folder_groups.setdefault(f["folder"], []).append(f["id"])

        for folder, file_ids in folder_groups.items():
            group = file_ids[:12]  # cap to avoid dense cliques
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    key = (min(group[i], group[j]), max(group[i], group[j]))
                    if key not in links:
                        links[key] = {"source": key[0], "target": key[1], "type": "folder", "weight": 0.5}

        return list(links.values())

    def _add_agent_role_links(self, all_files):
        """Connect agent profiles to their memory/readme/docs files."""
        links = []
        # Find agent profile files (README.md in agent folders)
        agent_profiles = {}
        agent_members = {}

        for f in all_files:
            path = f["id"]
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] == "agents":
                agent_name = parts[1]
                name_lower = f["name"].lower()
                if len(parts) == 3 and name_lower in ("readme", "memory", "manifest"):
                    # Use README as the primary profile, fallback to memory
                    if agent_name not in agent_profiles or name_lower == "readme":
                        agent_profiles[agent_name] = path
                agent_members.setdefault(agent_name, []).append(path)

        # Link agent profile → all files in that agent's folder
        for agent_name, profile_path in agent_profiles.items():
            for member_path in agent_members.get(agent_name, []):
                if member_path != profile_path:
                    links.append({
                        "source": profile_path,
                        "target": member_path,
                        "type": "agent-role",
                        "weight": 2.0,
                    })

        # Link manifest → all agent profiles
        manifest_id = "agents/manifest.md"
        for f in all_files:
            if f["id"] == manifest_id:
                for agent_name, profile_path in agent_profiles.items():
                    if profile_path != manifest_id:
                        links.append({
                            "source": manifest_id,
                            "target": profile_path,
                            "type": "agent-role",
                            "weight": 1.5,
                        })
                break

        return links

    def _add_agent_output_links(self, all_files):
        """Connect agents to their output/report files."""
        links = []
        file_ids = {f["id"] for f in all_files}

        for agent_name, prefixes in _AGENT_OUTPUT_PATTERNS.items():
            # Find the agent's profile file
            agent_profile = None
            for f in all_files:
                if f["id"].startswith(f"agents/{agent_name}/") and f["name"].lower() in ("readme", "memory"):
                    agent_profile = f["id"]
                    break

            if not agent_profile:
                continue

            # Find output files for this agent
            for f in all_files:
                for prefix in prefixes:
                    if f["id"].startswith(prefix):
                        links.append({
                            "source": agent_profile,
                            "target": f["id"],
                            "type": "agent-output",
                            "weight": 2.5,
                        })

        return links

    def _add_topic_similarity_links(self, all_files):
        """Connect files that share significant keywords."""
        links = []
        # Build keyword index
        file_keywords = {}
        for f in all_files:
            content = f.get("content", "") or ""
            # Also use the filename as context
            name_tokens = self._tokenize(f["name"])
            keywords = self._extract_keywords(content, top_n=20)
            keywords.update(name_tokens)
            if len(keywords) >= 3:
                file_keywords[f["id"]] = keywords

        # Compare pairs (only within same top-level folder or adjacent)
        file_ids = list(file_keywords.keys())
        for i in range(len(file_ids)):
            for j in range(i + 1, len(file_ids)):
                a, b = file_ids[i], file_ids[j]
                shared = file_keywords[a] & file_keywords[b]
                if len(shared) >= 4:  # at least 4 shared keywords
                    weight = min(len(shared) / 10.0, 1.0) * 1.5
                    links.append({
                        "source": a,
                        "target": b,
                        "type": "topic",
                        "weight": round(weight, 2),
                    })

        return links

    def _add_subfolder_links(self, all_files):
        """Connect parent folders to subfolder contents."""
        links = []
        folders_seen = set()
        for f in all_files:
            parts = f["id"].split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in folders_seen:
                    folders_seen.add(parent)
                    # Link parent folder representative → child file
                    links.append({
                        "source": f["id"],
                        "target": parent,
                        "type": "subfolder",
                        "weight": 0.3,
                    })
        return links

    def _add_date_links(self, all_files):
        """Connect daily notes that are chronologically adjacent."""
        links = []
        daily_files = []
        for f in all_files:
            if f["folder"] == "daily":
                try:
                    date_str = f["name"].replace(" ", "-").replace("_", "-")
                    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                    daily_files.append((dt, f["id"]))
                except (ValueError, IndexError):
                    pass

        daily_files.sort()
        for i in range(len(daily_files) - 1):
            links.append({
                "source": daily_files[i][1],
                "target": daily_files[i + 1][1],
                "type": "date-adjacent",
                "weight": 0.8,
            })

        return links

    def _add_project_context_links(self, all_files):
        """Connect project files to relevant context files."""
        links = []
        project_files = [f for f in all_files if f["folder"] in ("projects", "research")]
        context_files = [f for f in all_files if f["folder"] == "context"]

        for pf in project_files:
            p_keywords = self._extract_keywords(pf.get("content", "") or "", top_n=10)
            p_tokens = set(pf["name"].lower().split("-")) | set(pf["name"].lower().split("_"))
            p_keywords.update(p_tokens)

            for cf in context_files:
                c_keywords = self._extract_keywords(cf.get("content", "") or "", top_n=15)
                shared = p_keywords & c_keywords
                if len(shared) >= 3:
                    links.append({
                        "source": pf["id"],
                        "target": cf["id"],
                        "type": "project-context",
                        "weight": 1.0,
                    })

        return links

    # ── Rebuild Graph ─────────────────────────────────────────────────

    def rebuild_graph(self):
        """
        Full re-index: re-scan all files, extract enhanced links,
        rebuild the entire links table.
        """
        conn = self._conn()

        # Get all current files with content
        rows = conn.execute(
            "SELECT id, path, name, folder, content, size FROM files"
        ).fetchall()

        all_files = [
            {
                "id": r["id"],
                "path": r["path"],
                "name": r["name"],
                "folder": r["folder"],
                "content": r["content"] or "",
                "size": r["size"],
            }
            for r in rows
        ]

        # Clear all existing links
        conn.execute("DELETE FROM links")

        # Collect all link types
        all_links = []

        # Per-file links (wikilinks, tags)
        for f in all_files:
            file_links = self._extract_links(f["content"], f["id"])
            all_links.extend(file_links)

        # Cross-file links
        all_links.extend(self._add_folder_links(all_files))
        all_links.extend(self._add_agent_role_links(all_files))
        all_links.extend(self._add_agent_output_links(all_files))
        all_links.extend(self._add_topic_similarity_links(all_files))
        all_links.extend(self._add_date_links(all_files))
        all_links.extend(self._add_project_context_links(all_files))

        # Deduplicate: keep highest weight for same (source, type, target)
        deduped = {}
        for link in all_links:
            key = (link["source"], link["target"], link["type"])
            if key not in deduped or link["weight"] > deduped[key]["weight"]:
                deduped[key] = link

        # Insert into DB
        for link in deduped.values():
            # Skip self-links
            if link["source"] == link["target"]:
                continue
            conn.execute(
                "INSERT INTO links (source_id, target_id, link_type, weight) VALUES (?,?,?,?)",
                (link["source"], link["target"], link["type"], link["weight"]),
            )

        conn.commit()

        # Stats
        link_count = conn.execute("SELECT COUNT(*) as c FROM links").fetchone()["c"]
        type_counts = {}
        for row in conn.execute(
            "SELECT link_type, COUNT(*) as c FROM links GROUP BY link_type ORDER BY c DESC"
        ).fetchall():
            type_counts[row["link_type"]] = row["c"]

        conn.close()

        return {
            "ok": True,
            "files_indexed": len(all_files),
            "links_created": link_count,
            "by_type": type_counts,
        }

    # ── Standard CRUD ─────────────────────────────────────────────────

    def sync(self, vault_path=None):
        """Scan vault and update index. Only processes changed files."""
        vault = Path(vault_path) if vault_path else VAULT
        if not vault.exists():
            return {"synced": 0, "added": 0, "updated": 0, "removed": 0}

        conn = self._conn()
        now = datetime.now().isoformat()

        indexed = {}
        for row in conn.execute("SELECT id, path, hash, mtime FROM files"):
            indexed[row["path"]] = {"id": row["id"], "hash": row["hash"], "mtime": row["mtime"]}

        added, updated, removed = 0, 0, 0
        current_paths = set()

        for f in vault.rglob("*.md"):
            if f.name.startswith("."):
                continue
            rel = str(f.relative_to(vault))
            current_paths.add(rel)
            stat = f.stat()
            file_hash = self._file_hash(f)

            if rel in indexed:
                if indexed[rel]["hash"] != file_hash or indexed[rel]["mtime"] != stat.st_mtime:
                    content = f.read_text(errors="ignore")
                    conn.execute(
                        """UPDATE files SET name=?, folder=?, content=?, size=?, mtime=?, hash=?, updated_at=?
                           WHERE id=?""",
                        (f.stem, f.parent.name.lower(), content, stat.st_size, stat.st_mtime, file_hash, now, rel),
                    )
                    conn.execute("DELETE FROM links WHERE source_id=?", (rel,))
                    for link in self._extract_links(content, rel):
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, link_type, weight) VALUES (?,?,?,?)",
                            (link["source"], link["target"], link["type"], link["weight"]),
                        )
                    updated += 1
            else:
                content = f.read_text(errors="ignore")
                conn.execute(
                    """INSERT OR IGNORE INTO files (id, path, name, folder, content, size, mtime, hash, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (rel, rel, f.stem, f.parent.name.lower(), content, stat.st_size, stat.st_mtime, file_hash, now, now),
                )
                for link in self._extract_links(content, rel):
                    conn.execute(
                        "INSERT INTO links (source_id, target_id, link_type, weight) VALUES (?,?,?,?)",
                        (link["source"], link["target"], link["type"], link["weight"]),
                    )
                added += 1

        for rel in indexed:
            if rel not in current_paths:
                conn.execute("DELETE FROM files WHERE id=?", (rel,))
                conn.execute("DELETE FROM links WHERE source_id=? OR target_id=?", (rel, rel))
                removed += 1

        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)", (now,))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('file_count', ?)", (str(len(current_paths)),))

        conn.commit()
        conn.close()

        # Keep the cross-file knowledge graph enrichment fresh. sync() only adds
        # per-file wikilink/tag links; the rich folder/topic/agent-role links are
        # built by rebuild_graph(). Rebuild only when files actually changed so
        # read-only syncs (e.g. repeated get_vault_index() reads) stay cheap.
        changed = added + updated + removed
        if changed > 0:
            try:
                self.rebuild_graph()
            except Exception:
                pass

        return {
            "synced": added + updated,
            "added": added,
            "updated": updated,
            "removed": removed,
            "total": len(current_paths),
            "timestamp": now,
        }

    def index_file(self, fpath):
        """Index or re-index a single file."""
        fpath = Path(fpath) if not isinstance(fpath, Path) else fpath
        if not fpath.exists() or not fpath.is_file():
            return
        vault_path = str(fpath.resolve())
        rel = fpath.relative_to(VAULT)
        rel_str = str(rel)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        name = fpath.name
        content = ""
        if fpath.suffix.lower() == ".md":
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")[:50000]
            except Exception:
                pass
        size = fpath.stat().st_size
        mtime = datetime.fromtimestamp(fpath.stat().st_mtime).isoformat()
        file_hash = hashlib.md5(str(mtime).encode()).hexdigest()[:18]

        conn = self._conn()
        conn.execute("DELETE FROM files_fts WHERE rowid IN (SELECT rowid FROM files WHERE id=?)", (rel_str,))
        conn.execute("DELETE FROM files WHERE id=?", (rel_str,))
        conn.execute(
            "INSERT INTO files (id, path, name, folder, content, size, mtime, hash) VALUES (?,?,?,?,?,?,?,?)",
            (rel_str, vault_path, name, folder, content, size, mtime, file_hash),
        )
        conn.commit()
        conn.close()

    def remove_file(self, rel_path):
        """Remove a file from the index."""
        conn = self._conn()
        conn.execute("DELETE FROM files_fts WHERE rowid IN (SELECT rowid FROM files WHERE id=?)", (rel_path,))
        conn.execute("DELETE FROM files WHERE id=?", (rel_path,))
        conn.commit()
        conn.close()

    def search(self, query, max_results=10):
        """Full-text search using FTS5. Returns ranked results with snippets."""
        conn = self._conn()
        clean = re.sub(r"[^\w\s\-_]", " ", query).strip()
        if not clean:
            conn.close()
            return []

        try:
            rows = conn.execute(
                """
                SELECT f.id, f.path, f.name, f.folder, f.content, f.size,
                       fts.rank as score
                FROM files_fts fts
                JOIN files f ON f.rowid = fts.rowid
                WHERE files_fts MATCH ?
                ORDER BY fts.rank
                LIMIT ?
                """,
                (clean, max_results),
            ).fetchall()
        except Exception:
            like_q = f"%{clean}%"
            rows = conn.execute(
                """
                SELECT id, path, name, folder, content, size,
                       CASE
                           WHEN name LIKE ? THEN 50
                           WHEN content LIKE ? THEN 30
                           ELSE 10
                       END as score
                FROM files
                WHERE name LIKE ? OR content LIKE ?
                ORDER BY score DESC
                LIMIT ?
                """,
                (like_q, like_q, like_q, like_q, max_results),
            ).fetchall()

        results = []
        for row in rows:
            content = row["content"] or ""
            q_lower = clean.lower()
            idx = content.lower().find(q_lower)
            if idx >= 0:
                snippet = content[max(0, idx - 60) : idx + len(clean) + 60].replace("\n", " ").strip()
            else:
                snippet = content[:120].replace("\n", " ").strip()

            results.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"].replace("-", " ").replace("_", " ").title(),
                    "folder": row["folder"],
                    "score": row["score"],
                    "snippet": snippet,
                    "size": row["size"],
                }
            )

        conn.close()
        return results

    def semantic_search(self, query, top_k=10, backend=None):
        """
        Feature 6 — meaning-based search over the indexed vault.

        Pulls every indexed document and ranks by semantic similarity to the
        query (TF-IDF cosine by default; Ollama embeddings if SEMANTIC_BACKEND
        is set). Returns the same dict shape as search() for uniform UI
        rendering. Degrades to keyword search() if the corpus is empty.
        """
        try:
            from semantic_search import semantic_search as _sem
        except Exception:
            return self.search(query, max_results=top_k)

        conn = self._conn()
        rows = conn.execute(
            "SELECT id, path, name, folder, content, size FROM files"
        ).fetchall()
        conn.close()

        docs = [
            {
                "id": r["id"],
                "path": r["path"],
                "name": r["name"],
                "folder": r["folder"],
                "content": r["content"] or "",
                "size": r["size"],
            }
            for r in rows
        ]
        if not docs:
            return []
        try:
            return _sem(query, docs, top_k=top_k, backend=backend)
        except Exception:
            return self.search(query, max_results=top_k)

    def list_files(self, folder="all"):
        """List all files, optionally filtered by folder."""
        conn = self._conn()
        if folder and folder != "all":
            rows = conn.execute(
                "SELECT id, path, name, folder, size, mtime FROM files WHERE folder=? ORDER BY path",
                (folder,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, path, name, folder, size, mtime FROM files ORDER BY path").fetchall()

        results = [
            {
                "id": r["id"],
                "path": r["path"],
                "name": r["name"].replace("-", " ").replace("_", " ").title(),
                "folder": r["folder"],
                "size": r["size"],
                "mtime": r["mtime"],
            }
            for r in rows
        ]
        conn.close()
        return results

    def read_file(self, path):
        """Read a specific file's content."""
        conn = self._conn()
        row = conn.execute("SELECT * FROM files WHERE id=?", (path,)).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id": row["id"],
            "path": row["path"],
            "name": row["name"],
            "folder": row["folder"],
            "content": row["content"],
            "size": row["size"],
            "mtime": row["mtime"],
        }

    def stats(self):
        """Get vault statistics."""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) as c FROM files").fetchone()["c"]
        by_folder = {}
        for row in conn.execute("SELECT folder, COUNT(*) as c FROM files GROUP BY folder ORDER BY c DESC"):
            by_folder[row["folder"]] = row["c"]
        last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        link_count = conn.execute("SELECT COUNT(*) as c FROM links").fetchone()["c"]
        conn.close()
        return {
            "total_files": total,
            "by_folder": by_folder,
            "last_sync": last_sync["value"] if last_sync else None,
            "db_path": self.db_path,
            "total_links": link_count,
        }

    def graph(self):
        """Get knowledge graph data — nodes and links with rich metadata."""
        conn = self._conn()
        GROUP_COLORS = {
            "context": "#7c5bf5",
            "daily": "#5b9bf5",
            "projects": "#4ade80",
            "agents": "#fbbf24",
            "skills": "#f472b6",
            "loop": "#fb923c",
            "developer": "#60a5fa",
            "hermes": "#a78bfa",
            "researcher": "#34d399",
            "writer": "#f472b6",
            "seo": "#fbbf24",
            "threat": "#ef4444",
            "antigravity": "#818cf8",
            "templates": "#94a3b8",
            "reports": "#fb923c",
            "research": "#22d3ee",
            "kinlypro": "#4ade80",
        }

        LINK_COLORS = {
            "wikilink": "#7c5bf5",
            "tag": "#a78bfa",
            "folder": "#3b82f6",
            "agent-role": "#fbbf24",
            "agent-output": "#22c55e",
            "topic": "#f472b6",
            "subfolder": "#64748b",
            "date-adjacent": "#5b9bf5",
            "project-context": "#4ade80",
        }

        nodes = []
        for row in conn.execute("SELECT id, path, name, folder, size FROM files"):
            color = GROUP_COLORS.get(row["folder"], "#8888a0")
            nodes.append({
                "id": row["id"],
                "label": row["name"].replace("-", " ").replace("_", " ").title(),
                "group": row["folder"],
                "color": color,
                "path": row["path"],
                "size": max(4, min(14, (row["size"] or 0) / 200)),
            })

        links = []
        for row in conn.execute(
            "SELECT source_id, target_id, link_type, weight FROM links"
        ):
            links.append({
                "source": row["source_id"],
                "target": row["target_id"],
                "type": row["link_type"],
                "weight": row["weight"],
                "color": LINK_COLORS.get(row["link_type"], "#2a2a3a"),
            })

        # Create synthetic nodes for tag targets that don't match any file
        existing_ids = {n["id"] for n in nodes}
        tag_nodes = {}
        for l in links:
            if l["type"] == "tag" and l["target"] not in existing_ids:
                tag = l["target"]  # e.g. "#research"
                tag_label = tag[1:].replace("-", " ").replace("_", " ").title()
                tag_nodes[l["target"]] = {
                    "id": l["target"],
                    "label": tag_label,
                    "group": "tags",
                    "color": LINK_COLORS.get("tag", "#a78bfa"),
                    "path": "",
                    "size": 4,
                }
        nodes.extend(tag_nodes.values())
        # Build group summary for clusters
        groups = {}
        for n in nodes:
            g = n["group"]
            if g not in groups:
                groups[g] = {"name": g, "color": n["color"], "count": 0}
            groups[g]["count"] += 1

        conn.close()
        return {
            "nodes": nodes,
            "links": links,
            "groups": list(groups.values()),
            "stats": {"node_count": len(nodes), "link_count": len(links)},
        }

    def write_file(self, path, content, mode="write"):
        """Write or append to a file and update index."""
        fpath = VAULT / path
        if not fpath.resolve().is_relative_to(VAULT.resolve()):
            return {"error": "Path outside vault"}
        fpath.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now().isoformat()
        if mode == "append":
            existing = fpath.read_text(errors="ignore") if fpath.exists() else ""
            fpath.write_text(existing.rstrip() + "\n\n" + content + "\n")
        else:
            fpath.write_text(content + "\n")

        stat = fpath.stat()
        file_hash = self._file_hash(fpath)
        conn = self._conn()
        conn.execute(
            """INSERT OR REPLACE INTO files (id, path, name, folder, content, size, mtime, hash, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (path, path, fpath.stem, fpath.parent.name.lower(), fpath.read_text(errors="ignore"), stat.st_size, stat.st_mtime, file_hash, now, now),
        )
        conn.execute("DELETE FROM links WHERE source_id=?", (path,))
        for link in self._extract_links(content, path):
            conn.execute(
                "INSERT INTO links (source_id, target_id, link_type, weight) VALUES (?,?,?,?)",
                (link["source"], link["target"], link["type"], link["weight"]),
            )
        conn.commit()
        conn.close()
        return {"ok": True, "path": path}

    def get_backlinks(self, path):
        """Get all files that link to the given file."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT f.id, f.path, f.name, l.link_type FROM links l JOIN files f ON f.id = l.source_id WHERE l.target_id=?",
            (path,),
        ).fetchall()
        conn.close()
        return [{"id": r["id"], "path": r["path"], "name": r["name"], "link_type": r["link_type"]} for r in rows]

    # ── Agent Activity Log ────────────────────────────────────────────

    def log_activity(self, agent, action, task="", status="completed",
                     duration_ms=0, model="", details=""):
        """Log an agent action to the activity table."""
        from datetime import datetime
        now = datetime.now().isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO agent_activity (agent, action, task, status, duration_ms, model, details, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (agent, action, task, status, duration_ms, model, details, now),
        )
        conn.commit()
        conn.close()
        return True

    def get_activity(self, agent=None, status=None, limit=50):
        """Get activity log entries, optionally filtered."""
        conn = self._conn()
        query = "SELECT * FROM agent_activity WHERE 1=1"
        params = []
        if agent:
            query += " AND agent=?"
            params.append(agent)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_activity_stats(self):
        """Get aggregate stats from the activity log."""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) as c FROM agent_activity").fetchone()["c"]
        by_agent = {}
        for row in conn.execute(
            "SELECT agent, COUNT(*) as c, SUM(duration_ms) as total_ms FROM agent_activity GROUP BY agent ORDER BY c DESC"
        ).fetchall():
            by_agent[row["agent"]] = {"count": row["c"], "total_ms": row["total_ms"] or 0}
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) as c FROM agent_activity GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["c"]
        conn.close()
        return {"total_entries": total, "by_agent": by_agent, "by_status": by_status}

    def log_output(self, agent, task_id="", output_path="", output_type="text", quality_score=0):
        """Log an agent output artifact."""
        from datetime import datetime
        now = datetime.now().isoformat()
        conn = self._conn()
        conn.execute(
            """INSERT INTO agent_outputs (agent, task_id, output_path, output_type, quality_score, timestamp)
               VALUES (?,?,?,?,?,?)""",
            (agent, task_id, output_path, output_type, quality_score, now),
        )
        conn.commit()
        conn.close()
        return True

    def get_outputs(self, agent=None, limit=50):
        """Get logged outputs, optionally filtered by agent."""
        conn = self._conn()
        if agent:
            rows = conn.execute(
                "SELECT * FROM agent_outputs WHERE agent=? ORDER BY timestamp DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_outputs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Entity State Tracking (durable domain context graph) ──────────

    def touch_entity(self, entity_ref, state=None, task_id="",
                     goal_id=""):
        """Record a point-in-time snapshot of a domain entity's state,
        linked to the execution task that observed it.

        This provides the 'temporal validity' piece: query 'what was known
        about X when task T ran?' — the distinction between execution-graph
        state and context-graph state that the HydraDB article identifies
        as the gap in most agent systems.

        entity_ref: stable domain identifier ('project:quongtea',
                    'customer:123', 'agent:researcher')
        state:      dict or JSON string — what was true about the entity
        task_id:    the execution task ID that produced this knowledge
        goal_id:    the goal the task belongs to
        """
        try:
            import json as _json
            if isinstance(state, dict):
                state = _json.dumps(state, default=str)
            state = state or '{}'
            now = datetime.now().isoformat()
            conn = self._conn()
            conn.execute(
                """INSERT INTO entity_states
                   (entity_ref, state, task_id, goal_id, updated_at)
                   VALUES (?,?,?,?,?)""",
                (entity_ref, state, task_id or '', goal_id or '', now),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def get_entity_state(self, entity_ref, at_time=None):
        """Get the most recent state snapshot for an entity.

        at_time: optional ISO timestamp; if provided, returns the most
                 recent snapshot with updated_at <= at_time (historical
                 context — 'what was true at this moment?').

        Returns (state_dict, task_id, updated_at) or (None, None, None).
        """
        try:
            import json as _json
            conn = self._conn()
            if at_time:
                row = conn.execute(
                    """SELECT state, task_id, updated_at FROM entity_states
                       WHERE entity_ref=? AND updated_at <= ?
                       ORDER BY updated_at DESC LIMIT 1""",
                    (entity_ref, at_time),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT state, task_id, updated_at FROM entity_states
                       WHERE entity_ref=?
                       ORDER BY updated_at DESC LIMIT 1""",
                    (entity_ref,),
                ).fetchone()
            conn.close()
            if row:
                return (_json.loads(row["state"]), row["task_id"],
                        row["updated_at"])
            return (None, None, None)
        except Exception:
            return (None, None, None)

    def get_entity_timeline(self, entity_ref, limit=50):
        """Get all state snapshots for an entity in reverse chronological
        order. Useful for auditing: 'how did this entity's context evolve?'"""
        try:
            conn = self._conn()
            rows = conn.execute(
                """SELECT state, task_id, goal_id, updated_at
                   FROM entity_states WHERE entity_ref=?
                   ORDER BY updated_at DESC LIMIT ?""",
                (entity_ref, limit),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ── Convenience functions ────────────────────────────────────────────

_index = None

def get_index():
    global _index
    if _index is None:
        _index = VaultIndex()
        _index.sync()
    return _index

def search(query, max_results=10):
    return get_index().search(query, max_results)

def sync():
    return get_index().sync()

def rebuild_graph():
    return get_index().rebuild_graph()

def stats():
    return get_index().stats()

def graph():
    return get_index().graph()

def log_activity(agent, action, task="", status="completed", duration_ms=0, model="", details=""):
    return get_index().log_activity(agent, action, task, status, duration_ms, model, details)

def get_activity(agent=None, status=None, limit=50):
    return get_index().get_activity(agent, status, limit)

def get_activity_stats():
    return get_index().get_activity_stats()

def log_output(agent, task_id="", output_path="", output_type="text", quality_score=0):
    return get_index().log_output(agent, task_id, output_path, output_type, quality_score)

def get_outputs(agent=None, limit=50):
    return get_index().get_outputs(agent, limit)


if __name__ == "__main__":
    idx = VaultIndex()
    result = idx.sync()
    print(json.dumps(result, indent=2))
    print("\nStats:", json.dumps(idx.stats(), indent=2))
    print("\nRebuilding graph with enhanced links...")
    graph_result = idx.rebuild_graph()
    print(json.dumps(graph_result, indent=2))
