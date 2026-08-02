#!/usr/bin/env python3
"""
SerpAPI + Brave Search CLI for MoiraiCore researcher agent.

Usage:
    python3 search_tool.py "query" [--engine serpapi|brave|both] [--limit 5] [--json|--context]

Returns structured search results. Designed to be called by the task runner
or bridge before Hermes invocation, injecting results as pre-search context.
"""

import json
import sys
import os
import gzip
import ssl
import urllib.parse
import urllib.request
import argparse
from datetime import datetime
from pathlib import Path

# ── API Keys ──────────────────────────────────────────────────────
SERPAPI_KEY = os.environ.get(
    "SERPAPI_KEY",
    "28baa24363078071ddaa54f2b333631fb33d0fbdd6ec50e4623ccceef190d0c1"
)
BRAVE_KEY = os.environ.get(
    "BRAVE_API_KEY",
    "BSAD8WTgxjnK6hHxrDWZrfc98NygXkl"
)

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))

# ── SSL helper (macOS self-signed cert compat) ───────────────────
def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def _fetch_json(url: str, headers: dict, timeout: int = 15) -> dict:
    """HTTP GET that handles gzip and returns parsed JSON."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


# ── SerpAPI ───────────────────────────────────────────────────────
def search_serpapi(query: str, limit: int = 5, country: str = "au") -> list[dict]:
    """Search via SerpAPI (direct HTTP). Returns [{title, url, snippet, source, position, type}]."""
    params = urllib.parse.urlencode({
        "engine": "google", "q": query, "api_key": SERPAPI_KEY,
        "num": limit, "gl": country, "hl": "en",
    })
    try:
        raw = _fetch_json(f"https://serpapi.com/search.json?{params}",
                           {"User-Agent": "MoiraiCore/2.0"})
    except Exception as e:
        return [{"error": str(e), "source": "serpapi", "type": "error"}]

    items = []
    # Answer box first
    ab = raw.get("answer_box", {})
    if ab:
        items.append({
            "title": ab.get("title", ab.get("question", "Answer")),
            "url": ab.get("link", ""),
            "snippet": ab.get("answer", ab.get("snippet", "")),
            "source": "serpapi", "type": "answer_box",
        })
    # Knowledge graph
    kg = raw.get("knowledge_graph", {})
    if kg:
        items.append({
            "title": kg.get("title", ""),
            "url": kg.get("source", {}).get("link", ""),
            "snippet": kg.get("description", ""),
            "source": "serpapi", "type": "knowledge_graph",
        })
    # Organic results
    for r in raw.get("organic_results", [])[:limit]:
        items.append({
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "snippet": r.get("snippet", ""),
            "source": "serpapi",
            "position": r.get("position", 0),
            "type": "organic",
        })
    return items


# ── Brave Search ──────────────────────────────────────────────────
def search_brave(query: str, limit: int = 5) -> list[dict]:
    """Search via Brave Search API v1. Returns [{title, url, snippet, source}]."""
    import urllib.parse, urllib.request
    url = (
        f"https://api.search.brave.com/res/v1/web/search"
        f"?q={urllib.parse.quote(query)}&count={limit}&country=AU&search_lang=en"
    )
    try:
        data = _fetch_json(url, {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_KEY,
            "User-Agent": "MoiraiCore/2.0",
        })
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", "")[:300],
                "source": "brave",
            }
            for r in data.get("web", {}).get("results", [])[:limit]
        ]
    except Exception as e:
        return [{"error": str(e), "source": "brave", "type": "error"}]


# ── Utilities ─────────────────────────────────────────────────────
def deduplicate(results: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            out.append(r)
    return out


def format_context_block(results: list[dict], query: str = "") -> str:
    """Format as Hermes-injectable context block."""
    if not results:
        return ""
    header = f"## 🔍 Pre-Searched Results"
    if query:
        header += f' — "{query}"'
    lines = [header + "\n"]
    for i, r in enumerate(results, 1):
        rtype = r.get("type", "organic")
        prefix = {"knowledge_graph": "📊", "answer_box": "💡"}.get(rtype, f"{i}.")
        lines.append(f"### {prefix} {r.get('title', 'Untitled')}")
        if r.get("url"):
            lines.append(f"**URL:** {r['url']}")
        snippet = r.get("snippet", "")[:250]
        if snippet:
            lines.append(f"\n{snippet}")
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────
def main():
    import json as _json, argparse
    parser = argparse.ArgumentParser(description="Web search for MoiraiCore agents")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--engine", choices=["serpapi", "brave", "both"], default="both")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--gl", default="au", help="Country for SerpAPI")
    parser.add_argument("--json", action="store_true", help="Raw JSON output")
    parser.add_argument("--context", action="store_true", help="Hermes context block")
    args = parser.parse_args()

    results = []
    if args.engine in ("serpapi", "both"):
        results.extend(search_serpapi(args.query, args.limit, args.gl))
    if args.engine in ("brave", "both"):
        results.extend(search_brave(args.query, args.limit))
    results = deduplicate(results)

    if args.context:
        print(format_context_block(results, args.query))
    elif args.json:
        print(_json.dumps({
            "query": args.query, "engine": args.engine,
            "count": len(results), "timestamp": datetime.now().isoformat(),
            "results": results,
        }, indent=2))
    else:
        for i, r in enumerate(results, 1):
            print(f"{i}. {r.get('title', 'Untitled')}  [{r.get('source','')}]")
            print(f"   {r.get('url', '')}")
            s = r.get("snippet", "")[:120]
            if s: print(f"   {s}")
            print()


if __name__ == "__main__":
    main()
