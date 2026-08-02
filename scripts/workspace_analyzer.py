#!/usr/bin/env python3
"""
Workspace Analyzer — File detection, type classification, content extraction.
Scans the workspace directory and produces categorized file listings with
metadata for the dashboard gallery view.
"""

import os
import json
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", str(Path(__file__).resolve().parents[1] / "workspace"))

# ── File type definitions ────────────────────────────────────────────────────

CATEGORY_MAP = {
    "reports": {
        "extensions": [".md", ".txt", ".rst", ".adoc"],
        "icon": "📄",
        "color": "#7c5bf5",
        "description": "Reports, notes, and documents",
    },
    "data": {
        "extensions": [".json", ".csv", ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg"],
        "icon": "📊",
        "color": "#5b9bf5",
        "description": "Structured data and configuration",
    },
    "code": {
        "extensions": [".py", ".js", ".ts", ".sh", ".bash", ".zsh", ".sql", ".rb", ".go", ".rs", ".c", ".cpp", ".h", ".java", ".swift"],
        "icon": "💻",
        "color": "#4ade80",
        "description": "Source code and scripts",
    },
    "web": {
        "extensions": [".html", ".htm", ".css", ".scss", ".svg"],
        "icon": "🌐",
        "color": "#fbbf24",
        "description": "Web pages and stylesheets",
    },
    "images": {
        "extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff"],
        "icon": "🖼️",
        "color": "#f472b6",
        "description": "Images and graphics",
    },
    "documents": {
        "extensions": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods"],
        "icon": "📑",
        "color": "#fb923c",
        "description": "Office documents and PDFs",
    },
    "archives": {
        "extensions": [".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"],
        "icon": "🗜️",
        "color": "#8888a0",
        "description": "Compressed archives",
    },
    "media": {
        "extensions": [".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".avi", ".mkv", ".flac"],
        "icon": "🎬",
        "color": "#a78bfa",
        "description": "Audio and video files",
    },
}

# Build reverse lookup: extension -> category
EXT_TO_CATEGORY = {}
for cat, info in CATEGORY_MAP.items():
    for ext in info["extensions"]:
        EXT_TO_CATEGORY[ext.lower()] = cat


def classify_file(filepath):
    """Classify a file into a category based on extension and content."""
    ext = Path(filepath).suffix.lower()
    category = EXT_TO_CATEGORY.get(ext, "other")

    # Refine: markdown files with specific patterns
    if ext == ".md":
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(500)
                if any(head.startswith(p) for p in ["---", "```", "# ", "## "]):
                    category = "reports"
        except Exception:
            pass

    return category


def get_file_icon(filepath):
    """Get the appropriate icon for a file."""
    category = classify_file(filepath)
    return CATEGORY_MAP.get(category, {"icon": "📁"})["icon"]


def get_file_color(filepath):
    """Get the category color for a file."""
    category = classify_file(filepath)
    return CATEGORY_MAP.get(category, {"color": "#8888a0"})["color"]


def extract_preview(filepath, max_lines=30):
    """Extract a text preview from a file for the gallery card."""
    ext = Path(filepath).suffix.lower()

    # Text-based files
    if ext in (".md", ".txt", ".rst", ".adoc", ".json", ".csv", ".yaml", ".yml",
               ".xml", ".toml", ".ini", ".cfg", ".py", ".js", ".ts", ".sh",
               ".bash", ".zsh", ".sql", ".rb", ".go", ".rs", ".html", ".htm", ".css"):
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append("…")
                        break
                    stripped = line.rstrip()
                    if stripped:
                        lines.append(stripped)
                return "\n".join(lines) if lines else "(empty file)"
        except Exception as e:
            return f"(error reading: {e})"

    # Binary files
    mime, _ = mimetypes.guess_type(filepath)
    if mime:
        if mime.startswith("image/"):
            return f"[image:{mime}]"
        if mime.startswith("video/"):
            return f"[video:{mime}]"
        if mime.startswith("audio/"):
            return f"[audio:{mime}]"
        if mime == "application/pdf":
            return "[pdf document]"

    return f"[binary:{mime or 'unknown'}]"


def extract_thumbnail_text(filepath):
    """Extract a short summary line for the gallery card subtitle."""
    ext = Path(filepath).suffix.lower()

    if ext == ".md":
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                        return stripped[:120]
                    if stripped.startswith("# "):
                        return stripped.lstrip("# ").strip()[:120]
        except Exception:
            pass

    if ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"):
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(200)
                lines = [l for l in content.split("\n") if l.strip() and not l.strip().startswith("#")]
                if lines:
                    return lines[0][:120]
        except Exception:
            pass

    if ext in (".py", ".js", ".ts", ".sh", ".rb", ".go", ".rs"):
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(("def ", "class ", "function ", "import ", "from ", "#!/")):
                        return stripped[:120]
        except Exception:
            pass

    return ""


def scan_workspace(workspace_dir=None):
    """Scan the workspace directory and return categorized file listings."""
    ws_dir = Path(workspace_dir or WORKSPACE_DIR)
    if not ws_dir.exists():
        return {"categories": {}, "files": [], "total": 0, "workspace_dir": str(ws_dir)}

    files = []
    for entry in sorted(ws_dir.rglob("*"), key=lambda e: e.stat().st_mtime, reverse=True):
        if entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.parent != ws_dir:
            pass  # file is in subdirectory — still include it

        stat = entry.stat()
        category = classify_file(str(entry))
        ext = entry.suffix.lower()

        file_info = {
            "name": entry.name,
            "path": entry.name,
            "full_path": str(entry),
            "extension": ext,
            "category": category,
            "icon": get_file_icon(str(entry)),
            "color": get_file_color(str(entry)),
            "size": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "modified_human": _human_time(stat.st_mtime),
            "preview": extract_preview(str(entry)),
            "thumbnail_text": extract_thumbnail_text(str(entry)),
        }
        files.append(file_info)

    # Group by category
    categories = {}
    for f in files:
        cat = f["category"]
        if cat not in categories:
            categories[cat] = {
                "name": cat,
                "icon": CATEGORY_MAP.get(cat, {"icon": "📁"})["icon"],
                "color": CATEGORY_MAP.get(cat, {"color": "#8888a0"})["color"],
                "description": CATEGORY_MAP.get(cat, {"description": "Other files"})["description"],
                "files": [],
                "count": 0,
            }
        categories[cat]["files"].append(f)
        categories[cat]["count"] += 1

    return {
        "categories": categories,
        "files": files,
        "total": len(files),
        "workspace_dir": str(ws_dir),
    }


def get_file_content(filepath, workspace_dir=None):
    """Get the full content of a workspace file (safe path resolution)."""
    ws_dir = Path(workspace_dir or WORKSPACE_DIR).resolve()
    target = (ws_dir / filepath).resolve()

    # Security: ensure the resolved path is inside the workspace
    if not str(target).startswith(str(ws_dir)):
        return {"error": "Access denied: path outside workspace", "status": 403}

    if not target.exists():
        return {"error": "File not found", "status": 404}

    if not target.is_file():
        return {"error": "Not a file", "status": 400}

    mime, _ = mimetypes.guess_type(str(target))
    ext = target.suffix.lower()

    # Text files: return content
    is_text = (
        ext in (".md", ".txt", ".rst", ".adoc", ".json", ".csv", ".yaml", ".yml",
                ".xml", ".toml", ".ini", ".cfg", ".py", ".js", ".ts", ".sh",
                ".bash", ".zsh", ".sql", ".rb", ".go", ".rs", ".html", ".htm",
                ".css", ".scss", ".svg", ".log", ".env", ".gitignore")
        or (mime and mime.startswith("text/"))
    )

    if is_text:
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {
                "name": target.name,
                "path": filepath,
                "content": content,
                "mime": mime or "text/plain",
                "extension": ext,
                "size": target.stat().st_size,
                "is_text": True,
                "status": 200,
            }
        except Exception as e:
            return {"error": str(e), "status": 500}

    # Binary files: return metadata only
    return {
        "name": target.name,
        "path": filepath,
        "content": None,
        "mime": mime or "application/octet-stream",
        "extension": ext,
        "size": target.stat().st_size,
        "is_text": False,
        "status": 200,
    }


def _human_size(size):
    """Convert bytes to human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _human_time(timestamp):
    """Convert timestamp to human-readable relative time."""
    now = datetime.now(tz=timezone.utc).timestamp()
    diff = now - timestamp
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    if diff < 604800:
        return f"{int(diff / 86400)}d ago"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%b %d")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2 or sys.argv[1] == "scan":
        result = scan_workspace()
        print(json.dumps(result, indent=2, default=str))

    elif sys.argv[1] == "categories":
        result = scan_workspace()
        cats = result.get("categories", {})
        for name, info in cats.items():
            print(f"  {info['icon']} {name}: {info['count']} files")
            for f in info["files"][:3]:
                print(f"    - {f['name']} ({f['size_human']})")
            if info["count"] > 3:
                print(f"    … and {info['count'] - 3} more")

    elif sys.argv[1] == "file" and len(sys.argv) > 2:
        result = get_file_content(sys.argv[2])
        if result.get("is_text"):
            print(result["content"])
        else:
            print(json.dumps(result, indent=2, default=str))

    elif sys.argv[1] == "preview" and len(sys.argv) > 2:
        ws_dir = Path(WORKSPACE_DIR)
        target = ws_dir / sys.argv[2]
        if target.exists():
            print(extract_preview(str(target)))
        else:
            print("File not found")

    else:
        print("Usage: workspace_analyzer.py [scan|categories|file <path>|preview <path>]")
