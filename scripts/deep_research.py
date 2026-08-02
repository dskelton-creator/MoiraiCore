#!/usr/bin/env python3
"""
MoiraiCore — Deep Research Engine
=================================
Multi-round autonomous research that searches the web, fetches sources,
analyzes content, and synthesizes findings into a visual HTML report.

How it works:
  1. Takes a research question
  2. Runs N rounds of: search → fetch top results → extract key info → assess confidence
  3. Stops when confidence threshold is met or max rounds reached
  4. Generates a structured HTML report with citations
  5. Saves report to workspace and returns the path

Usage:
    python3 deep_research.py "Compare G-Brain vs Karpathy's LLM Wiki for personal knowledge bases"
    python3 deep_research.py "ASX mining sector outlook 2026" --rounds 5 --search brave
    python3 deep_research.py "KinlyPro competitor landscape" --notify --save-pdf

Integration:
    Called via workflow_engine.py deep_research node type, or standalone CLI.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from html import escape as html_escape

# ── SSL fix for macOS ──────────────────────────────────────────
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def _urlopen(url, timeout=10, headers=None):
    """URL open with SSL fix for macOS. Accepts URL string or Request object."""
    if isinstance(url, str):
        url = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(url, timeout=timeout, context=_ssl_ctx)

# Load .env file so API keys are available when run standalone or as subprocess
_env_file = Path(__file__).resolve().parents[1] / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _k.strip() and _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
WORKSPACE = AGENT_OS_ROOT / "workspace"
REPORTS_DIR = WORKSPACE / "deep-research"
NOTIF_DIR = WORKSPACE / "notifications"

# Ensure hermes_bridge is importable
sys.path.insert(0, str(SCRIPTS_DIR))


# ── Search backends ───

def search_duckduckgo(query: str, max_results: int = 8) -> list[dict]:
    """
    Search the web for results.
    Tries Startpage first (fast, no API key), falls back to DuckDuckGo HTML.
    Returns list of {"title": ..., "url": ..., "snippet": ...}.
    """
    # Primary: Startpage (Google results, no API key, fast)
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "15",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
             "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
             f"https://www.startpage.com/sp/search?query={urllib.parse.quote(query)}&cat=web"],
            capture_output=True, text=True, timeout=20
        )
        html = result.stdout
        results = []

        # Parse Startpage results
        for block in re.finditer(
            r'<a[^>]+class="[^"]*result__url[^"]*"[^>]+href="([^"]+)"[^>]*>.*?'
            r'<a[^>]+class="[^"]*result__title[^"]*"[^>]*>(.*?)</a>',
            html, re.DOTALL
        ):
            url = block.group(1)
            title = re.sub(r"<[^>]+>", "", block.group(2)).strip()
            if url.startswith("http") and title:
                results.append({"title": title, "url": url, "snippet": ""})

        # Fallback pattern for Startpage
        if not results:
            links = re.findall(r'class="result__url"[^>]+href="([^"]+)"', html)
            titles = re.findall(r'class="result__title"[^>]*>(.*?)</a>', html, re.DOTALL)
            for i, url in enumerate(links):
                title = re.sub(r"<[^>]+>", "", titles[i]).strip() if i < len(titles) else ""
                if url.startswith("http") and title:
                    results.append({"title": title, "url": url, "snippet": ""})

        if results:
            return results[:max_results]
    except Exception:
        pass

    # Fallback: DuckDuckGo HTML
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "15",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
             f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode({'q': query})}"],
            capture_output=True, text=True, timeout=20
        )
        html = result.stdout
        if "anomaly-modal" not in html:
            results = []
            for block in re.finditer(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.DOTALL
            ):
                url_raw = block.group(1)
                title = re.sub(r"<[^>]+>", "", block.group(2)).strip()
                actual_url = url_raw
                if "duckduckgo.com/l/?" in url_raw:
                    m = re.search(r"uddg=([^&]+)", url_raw)
                    if m:
                        actual_url = urllib.parse.unquote(m.group(1))
                if actual_url.startswith("http") and title:
                    results.append({"title": title, "url": actual_url, "snippet": ""})
            if results:
                return results[:max_results]
    except Exception:
        pass

    return [{"error": "All search backends failed", "title": "Search failed", "url": "", "snippet": ""}]


def search_brave(query: str, api_key: str, max_results: int = 8) -> list[dict]:
    """
    Search Brave Web Search API.
    Requires BRAVE_API_KEY env var or config.
    """
    try:
        url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={max_results}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        })
        with _urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", "")[:300],
            })
        return results
    except Exception as e:
        # Log real error internally, return generic message to avoid leaking config details
        import sys as _sys
        print(f"[search_brave] error: {e}", file=_sys.stderr)
        return [{"error": "Search backend unavailable", "title": "Brave search failed", "url": "", "snippet": ""}]


def search_duckduckgo_api(query: str, max_results: int = 8) -> list[dict]:
    """
    Search DuckDuckGo via their instant answer API (free, no key needed).
    Fast but returns fewer results than full web search.
    """
    try:
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "MoiraiCore-OS/2.0"})
        with _urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        results = []
        if data.get("Abstract"):
            results.append({
                "title": data.get("Heading", query),
                "url": data.get("AbstractURL", ""),
                "snippet": data.get("Abstract", "")[:300],
            })
        for topic in data.get("RelatedTopics", []):
            if isinstance(topic, dict) and topic.get("FirstURL"):
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "url": topic["FirstURL"],
                    "snippet": topic.get("Text", "")[:200],
                })
            elif isinstance(topic, dict) and "Topics" in topic:
                for sub in topic["Topics"][:2]:
                    if sub.get("FirstURL"):
                        results.append({
                            "title": sub.get("Text", "")[:80],
                            "url": sub["FirstURL"],
                            "snippet": sub.get("Text", "")[:200],
                        })
        return results[:max_results]
    except Exception:
        return []


def search(query: str, backend: str = "auto", max_results: int = 8) -> list[dict]:
    """
    Unified search interface.
    
    Backends:
      - "auto"     : Try DuckDuckGo API first (fast), then Hermes web_search (thorough)
      - "brave"    : Brave Search API (requires BRAVE_API_KEY)
      - "duckduckgo": DuckDuckGo instant answer API only (fast, limited)
      - "hermes"   : Hermes web_search via CLI (~15-20s, most thorough)
    """
    # Try DuckDuckGo API first (fast, no API key)
    if backend in ("auto", "duckduckgo"):
        results = search_duckduckgo_api(query, max_results)
        if results and len(results) >= 2:
            return results

    # Try Brave API
    if backend in ("auto", "brave"):
        brave_key = os.environ.get("BRAVE_API_KEY", "")
        if brave_key:
            results = search_brave(query, brave_key, max_results)
            if results and "error" not in results[0]:
                return results

    # Use Hermes web_search (slow but reliable)
    if backend in ("auto", "hermes"):
        return _search_via_hermes(query, max_results)

    return []


def _search_via_hermes(query: str, max_results: int = 8) -> list[dict]:
    """
    Search via Hermes CLI web_search tool.
    Uses direct subprocess to avoid ICE overhead.
    Note: This is slow (~15-20s per call) but reliable.
    """
    try:
        HERMES_CLI = os.environ.get("HERMES_CLI", "")
        if not HERMES_CLI:
            _candidates = [
                os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes"),
                os.path.join(os.path.expanduser("~"), ".local", "bin", "hermes"),
            ]
            for _c in _candidates:
                if os.path.isfile(_c):
                    HERMES_CLI = _c
                    break
            else:
                HERMES_CLI = "hermes"
        search_prompt = (
            f"Use the web_search tool to search for: {query}\n\n"
            f"Return a JSON array of the top {max_results} results. "
            f"Each result should have: title, url, snippet. "
            f"Output ONLY the JSON array."
        )
        result = subprocess.run(
            [HERMES_CLI, "chat", "-q", search_prompt, "--quiet"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "ICE_DISABLED": "1"},
        )
        response = result.stdout.strip()
        # Parse JSON from response
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL)
        if not m:
            m = re.search(r"\[[\s\S]*\]", response)
        if m:
            data = json.loads(m.group(1) if "```" in m.group(0) else m.group(0))
            return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")} for r in data[:max_results]]
    except Exception:
        pass
    return []


# ── Content fetching ───

def fetch_url(url: str, max_chars: int = 4000) -> str:
    """
    Fetch a URL and extract readable text.
    Uses curl for better compatibility (handles redirects, compression, etc.).
    """
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "15",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             url],
            capture_output=True, text=True, timeout=20
        )
        html = result.stdout
        if not html:
            return ""

        # Strip HTML tags to get readable text
        # Remove scripts and styles first
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Remove remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Decode HTML entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")

        return text[:max_chars]
    except Exception as e:
        return f"[Fetch error: {e}]"


# ── Research round ───

def run_research_round(
    question: str,
    round_num: int,
    previous_findings: list[dict],
    search_backend: str = "duckduckgo",
    max_results: int = 6,
    fetch_depth: int = 3,
) -> dict:
    """
    Run a single research round: search → fetch → extract.
    Returns {"queries": [...], "sources": [...], "findings": [...], "round": N}.
    """
    round_data = {
        "round": round_num,
        "queries": [],
        "sources": [],
        "findings": [],
        "timestamp": datetime.now().isoformat(),
    }

    # Build search queries based on question and previous findings
    queries = _build_search_questions(question, previous_findings, round_num)

    all_sources = []
    all_findings = []

    for q in queries[:3]:  # Max 3 queries per round
        round_data["queries"].append(q)
        results = search(q, backend=search_backend, max_results=max_results)

        for r in results:
            if "error" in r or not r.get("url"):
                continue

            source = {
                "title": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
                "fetched": False,
                "content_preview": "",
                "round": round_num,
                "query": q,
            }

            # Fetch top N results for deeper analysis
            if len([s for s in all_sources if s["fetched"]]) < fetch_depth:
                content = fetch_url(r["url"])
                if content and "[Fetch error" not in content:
                    source["fetched"] = True
                    source["content_preview"] = content[:2000]

            all_sources.append(source)

    round_data["sources"] = all_sources
    return round_data


def _build_search_questions(question: str, previous_findings: list[dict], round_num: int) -> list[str]:
    """
    Build search queries for a research round.
    Round 1: Direct questions from the topic
    Round 2+: Refined questions based on gaps identified in previous rounds
    """
    queries = []

    if round_num == 1:
        # First round: direct search
        queries.append(question)
        # Add a broader context search
        words = question.split()
        if len(words) > 4:
            queries.append(" ".join(words[:4]) + " overview")
    else:
        # Later rounds: refine based on what we've found
        # Extract key terms from previous findings
        key_terms = set()
        for finding in previous_findings:
            for source in finding.get("sources", []):
                title = source.get("title", "")
                # Extract capitalized phrases as key terms
                for m in re.finditer(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", title):
                    term = m.group(0)
                    if len(term) > 3 and term.lower() not in question.lower():
                        key_terms.add(term)

        # Build targeted queries from key terms
        for term in list(key_terms)[:3]:
            queries.append(f"{term} {question[:50]}")

        # Always include the original question with "detailed" or "analysis"
        if round_num == 2:
            queries.append(f"{question} detailed analysis")
        elif round_num >= 3:
            queries.append(f"{question} comparison review 2026")

    return queries[:3]


# ── Synthesis ───

def synthesize_findings(question: str, all_rounds: list[dict], model: str = "hermes") -> dict:
    """
    Synthesize all research rounds into a structured report.
    Uses the Hermes bridge to generate the synthesis.
    """
    # Build the synthesis prompt
    sources_summary = []
    for round_data in all_rounds:
        for source in round_data.get("sources", []):
            if source.get("fetched"):
                sources_summary.append({
                    "title": source["title"],
                    "url": source["url"],
                    "preview": source.get("content_preview", "")[:500],
                    "round": source["round"],
                })

    # Build prompt for synthesis
    prompt_parts = [
        f"# Deep Research Synthesis",
        f"",
        f"## Research Question",
        f"{question}",
        f"",
        f"## Sources Analyzed ({len(sources_summary)} sources across {len(all_rounds)} rounds)",
        f"",
    ]

    for i, src in enumerate(sources_summary, 1):
        prompt_parts.append(f"### Source {i}: {src['title']}")
        prompt_parts.append(f"URL: {src['url']}")
        prompt_parts.append(f"```")
        prompt_parts.append(src["preview"][:400])
        prompt_parts.append(f"```")
        prompt_parts.append("")

    prompt_parts.extend([
        f"## Task",
        f"Based on the sources above, write a comprehensive research report that:",
        f"1. Directly answers the research question",
        f"2. Cites specific sources (use [Source N] notation)",
        f"3. Includes an executive summary (3-5 sentences)",
        f"4. Organizes findings into clear sections with headings",
        f"5. Notes any conflicting information between sources",
        f"6. Provides a 'Confidence Assessment' (High/Medium/Low) with reasoning",
        f"",
        f"Write the report in markdown. Be thorough and cite sources.",
    ])

    prompt = "\n".join(prompt_parts)

    # Call Hermes for synthesis
    try:
        from hermes_bridge import run_hermes
        result = run_hermes(["chat", "-q", prompt, "--quiet"], timeout=180)
        synthesis = result.get("response", "")
        session_id = result.get("session_id")
    except Exception as e:
        synthesis = f"Synthesis error: {e}\n\nRaw sources:\n" + "\n".join(
            f"- {s['title']}: {s['url']}" for s in sources_summary
        )
        session_id = None

    return {
        "synthesis": synthesis,
        "session_id": session_id,
        "sources_used": len(sources_summary),
        "rounds": len(all_rounds),
    }


# ── HTML Report Generation ─

def generate_html_report(question: str, synthesis: dict, all_rounds: list[dict]) -> str:
    """
    Generate a visual HTML report from the research synthesis.
    Returns the HTML string.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_sources = sum(len(r.get("sources", [])) for r in all_rounds)
    fetched_sources = sum(
        1 for r in all_rounds for s in r.get("sources", []) if s.get("fetched")
    )

    # Build sources list HTML
    sources_html = []
    seen_urls = set()
    source_num = 0
    for round_data in all_rounds:
        for source in round_data.get("sources", []):
            url = source.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                source_num += 1
                sources_html.append(f"""
                <li class="source-item">
                    <span class="source-num">[{source_num}]</span>
                    <a href="{html_escape(url)}" target="_blank">{html_escape(source.get("title", url))}</a>
                    <span class="source-url">{html_escape(url[:80])}{"..." if len(url) > 80 else ""}</span>
                </li>""")

    # Convert synthesis markdown to HTML (basic)
    synthesis_html = _markdown_to_html(synthesis.get("synthesis", ""))

    # Build rounds summary
    rounds_html = []
    for round_data in all_rounds:
        rounds_html.append(f"""
        <div class="round-summary">
            <h4>Round {round_data['round']}</h4>
            <p>Queries: {', '.join(html_escape(q) for q in round_data.get("queries", []))}</p>
            <p>Sources found: {len(round_data.get("sources", []))} | Fetched: {sum(1 for s in round_data.get("sources", []) if s.get("fetched"))}</p>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Deep Research: {html_escape(question[:60])}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f0f14; color: #e0e0e8; line-height: 1.7; }}
    .container {{ max-width: 900px; margin: 0 auto; padding: 40px 20px; }}
    .header {{ border-bottom: 2px solid #7c5bf5; padding-bottom: 20px; margin-bottom: 30px; }}
    .header h1 {{ font-size: 1.8em; color: #7c5bf5; margin-bottom: 8px; }}
    .header .meta {{ color: #888; font-size: 0.9em; }}
    .stats {{ display: flex; gap: 20px; margin: 20px 0; flex-wrap: wrap; }}
    .stat {{ background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 8px; padding: 12px 20px; text-align: center; }}
    .stat .num {{ font-size: 1.5em; font-weight: bold; color: #7c5bf5; }}
    .stat .label {{ font-size: 0.8em; color: #888; }}
    .synthesis {{ background: #14141e; border: 1px solid #2a2a3a; border-radius: 12px; padding: 30px; margin: 30px 0; }}
    .synthesis h2 {{ color: #7c5bf5; margin: 20px 0 10px; }}
    .synthesis h3 {{ color: #5b9bf5; margin: 15px 0 8px; }}
    .synthesis p {{ margin: 10px 0; }}
    .synthesis ul, .synthesis ol {{ margin: 10px 0 10px 25px; }}
    .synthesis li {{ margin: 5px 0; }}
    .synthesis code {{ background: #1a1a24; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
    .synthesis blockquote {{ border-left: 3px solid #7c5bf5; padding-left: 15px; margin: 15px 0; color: #aaa; }}
    .sources {{ background: #14141e; border: 1px solid #2a2a3a; border-radius: 12px; padding: 25px; margin: 30px 0; }}
    .sources h2 {{ color: #7c5bf5; margin-bottom: 15px; }}
    .source-item {{ padding: 8px 0; border-bottom: 1px solid #1a1a24; }}
    .source-item:last-child {{ border-bottom: none; }}
    .source-num {{ color: #7c5bf5; font-weight: bold; margin-right: 8px; }}
    .source-item a {{ color: #5b9bf5; text-decoration: none; }}
    .source-item a:hover {{ text-decoration: underline; }}
    .source-url {{ display: block; color: #666; font-size: 0.85em; margin-left: 30px; }}
    .rounds {{ margin: 20px 0; }}
    .round-summary {{ background: #1a1a24; border-radius: 8px; padding: 12px 16px; margin: 8px 0; }}
    .round-summary h4 {{ color: #fbbf24; }}
    .round-summary p {{ color: #888; font-size: 0.9em; margin: 4px 0; }}
    .footer {{ border-top: 1px solid #2a2a3a; padding-top: 20px; margin-top: 40px; color: #666; font-size: 0.85em; text-align: center; }}
    @media print {{ body {{ background: white; color: black; }} .header h1 {{ color: #333; }} }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🔬 Deep Research Report</h1>
        <p class="question">{html_escape(question)}</p>
        <p class="meta">Generated: {now} | MoiraiCore Deep Research Engine</p>
    </div>

    <div class="stats">
        <div class="stat"><div class="num">{synthesis.get("rounds", 0)}</div><div class="label">Research Rounds</div></div>
        <div class="stat"><div class="num">{total_sources}</div><div class="label">Sources Found</div></div>
        <div class="stat"><div class="num">{fetched_sources}</div><div class="label">Sources Analyzed</div></div>
        <div class="stat"><div class="num">{synthesis.get("sources_used", 0)}</div><div class="label">Used in Synthesis</div></div>
    </div>

    <div class="synthesis">
        {synthesis_html}
    </div>

    <div class="rounds">
        <h2>📊 Research Process</h2>
        {"".join(rounds_html)}
    </div>

    <div class="sources">
        <h2>📚 All Sources</h2>
        <ul>
            {"".join(sources_html) if sources_html else "<li>No sources found</li>"}
        </ul>
    </div>

    <div class="footer">
        MoiraiCore Deep Research Engine • {now}<br>
        Question: {html_escape(question)}
    </div>
</div>
</body>
</html>"""

    return html


def _markdown_to_html(md: str) -> str:
    """Basic markdown to HTML converter."""
    if not md:
        return "<p><em>No synthesis available.</em></p>"

    html = html_escape(md)

    # Headers
    html = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", html, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)

    # Bold and italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)

    # Code blocks
    html = re.sub(r"```(\w*)\n(.*?)```", r"<pre><code>\2</code></pre>", html, flags=re.DOTALL)

    # Inline code
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)

    # Lists
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = re.sub(r"(<li>.*</li>\n?)+", r"<ul>\g<0></ul>", html)

    # Numbered lists
    html = re.sub(r"^\d+\. (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)

    # Blockquotes
    html = re.sub(r"^> (.+)$", r"<blockquote>\1</blockquote>", html, flags=re.MULTILINE)

    # Paragraphs (lines that aren't already wrapped)
    lines = html.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("<"):
            result.append(f"<p>{stripped}</p>")
        else:
            result.append(line)

    return "\n".join(result)


# ── Main Research Orchestrator ───

class DeepResearchEngine:
    """
    Main Deep Research orchestrator.

    Usage:
        engine = DeepResearchEngine()
        result = engine.research("Compare G-Brain vs Karpathy's LLM Wiki")
        # result = {"report_path": "...", "html": "...", "sources": 15, "rounds": 3, ...}
    """

    def __init__(self, search_backend: str = "duckduckgo", max_rounds: int = 3,
                 max_results: int = 6, fetch_depth: int = 3):
        self.search_backend = search_backend
        self.max_rounds = max_rounds
        self.max_results = max_results
        self.fetch_depth = fetch_depth

    def research(self, question: str, notify: bool = False) -> dict:
        """
        Run full deep research on a question.
        Returns dict with report_path, html, stats.
        """
        started_at = datetime.now().isoformat()
        all_rounds = []
        all_findings = []

        print(f"🔬 Deep Research: {question}")
        print(f"   Max rounds: {self.max_rounds} | Search: {self.search_backend}")
        print()

        for round_num in range(1, self.max_rounds + 1):
            print(f"── Round {round_num}/{self.max_rounds} ──")

            round_data = run_research_round(
                question=question,
                round_num=round_num,
                previous_findings=all_findings,
                search_backend=self.search_backend,
                max_results=self.max_results,
                fetch_depth=self.fetch_depth,
            )

            all_rounds.append(round_data)
            all_findings.append(round_data)

            sources_found = len(round_data.get("sources", []))
            sources_fetched = sum(1 for s in round_data.get("sources", []) if s.get("fetched"))
            print(f"   Queries: {len(round_data.get('queries', []))}")
            print(f"   Sources: {sources_found} found, {sources_fetched} fetched")

            # Check if we have enough information (simple heuristic)
            total_fetched = sum(
                1 for r in all_rounds for s in r.get("sources", []) if s.get("fetched")
            )
            if total_fetched >= 5 and round_num >= 2:
                print(f"   ✅ Sufficient sources gathered ({total_fetched} fetched). Stopping early.")
                break

            # Small delay between rounds to be respectful
            if round_num < self.max_rounds:
                time.sleep(1)

        # Synthesize
        print()
        print("── Synthesizing ──")
        synthesis = synthesize_findings(question, all_rounds)
        print(f"   Sources used: {synthesis.get('sources_used', 0)}")

        # Generate HTML report
        html = generate_html_report(question, synthesis, all_rounds)

        # Save report
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = re.sub(r"[^\w\-]", "_", question[:40]).strip("_")
        report_path = REPORTS_DIR / f"research_{ts}_{safe_name}.html"
        report_path.write_text(html)

        # Also save JSON data
        data_path = REPORTS_DIR / f"research_{ts}_{safe_name}.json"
        data_path.write_text(json.dumps({
            "question": question,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "rounds": all_rounds,
            "synthesis": synthesis,
            "report_path": str(report_path),
        }, indent=2, default=str))

        total_sources = sum(len(r.get("sources", [])) for r in all_rounds)

        print()
        print(f"✅ Report saved: {report_path}")
        print(f"   Total sources: {total_sources}")
        print(f"   Rounds: {len(all_rounds)}")

        # Notification
        if notify:
            NOTIF_DIR.mkdir(parents=True, exist_ok=True)
            notif_file = NOTIF_DIR / f"research-complete-{ts}.txt"
            notif_file.write_text(
                f"Deep Research complete: {question}\n"
                f"Report: {report_path}\n"
                f"Sources: {total_sources} | Rounds: {len(all_rounds)}\n"
            )

        return {
            "ok": True,
            "question": question,
            "report_path": str(report_path),
            "data_path": str(data_path),
            "html": html,
            "total_sources": total_sources,
            "rounds_run": len(all_rounds),
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
        }


# ── CLI ───

def main():
    parser = argparse.ArgumentParser(description="MoiraiCore — Deep Research Engine")
    parser.add_argument("question", help="The research question")
    parser.add_argument("--rounds", type=int, default=3, help="Max research rounds (default: 3)")
    parser.add_argument("--results", type=int, default=6, help="Max search results per query (default: 6)")
    parser.add_argument("--fetch-depth", type=int, default=3, help="Max pages to fetch per round (default: 3)")
    parser.add_argument("--search", choices=["duckduckgo", "brave"], default="duckduckgo", help="Search backend")
    parser.add_argument("--notify", action="store_true", help="Create notification when complete")
    parser.add_argument("--json", action="store_true", help="Output JSON result only")

    args = parser.parse_args()

    engine = DeepResearchEngine(
        search_backend=args.search,
        max_rounds=args.rounds,
        max_results=args.results,
        fetch_depth=args.fetch_depth,
    )

    result = engine.research(args.question, notify=args.notify)

    if args.json:
        # Don't include full HTML in JSON output
        out = {k: v for k, v in result.items() if k != "html"}
        print(json.dumps(out, indent=2, default=str))
    else:
        print()
        print("=" * 60)
        print(f"Report: {result['report_path']}")
        print(f"Open in browser: file://{result['report_path']}")
        print("=" * 60)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
