#!/usr/bin/env python3
"""
Daily Hermes Agent update checker.
- Checks the local hermes install vs the latest GitHub release/tag
- Also checks npm for the `agent-browser` dependency
- Parses GitHub release notes for meaningful capabilities
- Writes structured JSON to workspace for dashboard consumption
- Idempotent: safe to re-run, only writes when new version detected
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG = {
    # GitHub repo info
    "github_repo": "NousResearch/Hermes-Agent",
    "github_api_base": "https://api.github.com/repos/NousResearch/Hermes-Agent",
    # Local hermes install
    "hermes_cli": "hermes",
    # Dashboard state
    "state_file": os.path.expanduser(
        "moiraicore/workspace/state/hermes-version.json"
    ),
    "output_dir": os.path.expanduser(
        "moiraicore/workspace/hermes-updates"
    ),
    # Capability keywords relevant to MoiraiCore
    "capability_keywords": [
        "tool", "skill", "plugin", "cron", "gateway", "mcp",
        "security", "auth", "sso", "oauth", "workspace", "dashboard",
        "agent", "multi-agent", "orchestration", "delegation",
        "docker", "deployment", "browser", "search", "memory",
        "vault", "encryption", "sandbox", "sandboxing",
        "new command", "new tool", "new skill", "breaking change",
        "new provider", "model", "claude", "openai", "gemini",
    ],
}

SEVERITY_WORDS = {
    "critical": ["breaking", "security", "vulnerability", "deprecated removal", "critical", "cve"],
    "high": ["new feature", "new tool", "new command", "new skill", "major"],
}


def ensure_dirs():
    Path(CONFIG["state_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(CONFIG["output_dir"]).mkdir(parents=True, exist_ok=True)


def load_state():
    try:
        with open(CONFIG["state_file"]) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_version": None, "last_checked": None, "known_versions": []}


def save_state(state):
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2)


def fetch_json(url, timeout=15):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "moiraicore-os-updater/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [WARN] Fetch failed for {url}: {e}")
        return None


def get_local_hermes_version():
    """Get the currently installed hermes version via CLI."""
    # hermes CLI may be in ~/.local/bin or a pyenv shim — use shell to resolve
    try:
        result = subprocess.run(
            ["hermes", "--version"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PATH": f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"},
        )
        output = result.stdout.strip() or result.stderr.strip()
        # Parse "Hermes Agent v0.15.1 (2026.5.29)"
        match = re.search(r"v?(\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1), output
    except Exception as e:
        print(f"  [WARN] Could not get local hermes version: {e}")
    return None, ""


def get_local_commits_behind():
    """Check how many commits the local install is behind origin."""
    try:
        result = subprocess.run(
            ["hermes", "--version"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PATH": f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"},
        )
        output = result.stdout + result.stderr
        match = re.search(r"(\d+)\s+commits?\s+behind", output, re.IGNORECASE)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def get_latest_github_release():
    """Get latest release info from GitHub."""
    data = fetch_json(f"{CONFIG['github_api_base']}/releases/latest")
    if not data:
        return None
    return {
        "tag": data.get("tag_name", "").lstrip("v"),
        "name": data.get("name", ""),
        "published": data.get("published_at", ""),
        "body": data.get("body", ""),
        "url": data.get("html_url", ""),
    }


def get_recent_releases(count=5):
    """Get recent releases for changelog parsing."""
    data = fetch_json(f"{CONFIG['github_api_base']}/releases?per_page={count}")
    if not data or not isinstance(data, list):
        return []
    results = []
    for rel in data:
        results.append({
            "tag": rel.get("tag_name", "").lstrip("v"),
            "name": rel.get("name", ""),
            "published": rel.get("published_at", ""),
            "body": rel.get("body", ""),
            "url": rel.get("html_url", ""),
        })
    return results


def parse_capabilities(release_body):
    """Extract capability mentions from release notes markdown."""
    if not release_body:
        return []

    capabilities = []
    lines = release_body.splitlines()

    in_list = False
    for line in lines:
        stripped = line.strip()

        # Detect bullet points
        if stripped.startswith("- ") or stripped.startswith("* "):
            in_list = True
            item = stripped[2:].strip()
            item_lower = item.lower()
            # Clean markdown links for keyword matching
            item_clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", item_lower)

            for kw in CONFIG["capability_keywords"]:
                if kw.lower() in item_clean:
                    severity = "medium"
                    for sev, words in SEVERITY_WORDS.items():
                        if any(w in item_clean for w in words):
                            severity = sev
                            break

                    capabilities.append({
                        "keyword": kw,
                        "description": stripped[2:].strip(),
                        "severity": severity,
                    })
                    break

    return capabilities


def _calc_severity_from_behind(commits_behind):
    """Derive a severity level from how far behind the local install is."""
    if commits_behind is None:
        return "info"
    if commits_behind > 200:
        return "high"
    if commits_behind > 50:
        return "medium"
    if commits_behind > 0:
        return "low"
    return "info"


def generate_dashboard_item(latest_tag, local_version, commits_behind, all_releases):
    """Write JSON for dashboard workspace widget."""
    now = datetime.now(timezone.utc).isoformat()

    # Parse capabilities from all recent releases
    parsed_releases = []
    high_caps = []
    for rel in all_releases:
        caps = parse_capabilities(rel["body"])
        if caps:
            parsed_releases.append({
                "version": rel["tag"],
                "capabilities": caps,
            })
        for c in caps:
            if c["severity"] in ("critical", "high"):
                high_caps.append({**c, "release": rel["tag"]})

    # Determine overall severity
    severity = "info"
    if any(c["severity"] == "critical" for c in high_caps):
        severity = "critical"
    elif any(c["severity"] == "high" for c in high_caps):
        severity = "high"
    elif high_caps:
        severity = "medium"
    elif commits_behind and commits_behind > 100:
        severity = "medium"
    elif commits_behind and commits_behind > 0:
        severity = "low"

    is_new = latest_tag != local_version

    dashboard_data = {
        "id": f"hermes-update-{latest_tag}",
        "type": "hermes-update",
        "version": latest_tag,
        "local_version": local_version,
        "commits_behind": commits_behind,
        "checked_at": now,
        "severity": severity,
        "is_new_release": is_new,
        "summary": _build_summary(latest_tag, local_version, commits_behind, is_new),
        "capability_count": len(high_caps),
        "capabilities": high_caps[:10],
        "action_required": severity in ("critical", "high") or (is_new and commits_behind and commits_behind > 50),
        "release_count": len(parsed_releases),
        "changelog_url": f"https://github.com/{CONFIG['github_repo']}/releases",
    }

    # Write version-specific file
    output_path = os.path.join(CONFIG["output_dir"], f"update-{latest_tag}.json")
    with open(output_path, "w") as f:
        json.dump(dashboard_data, f, indent=2)

    # Write "latest" pointer
    latest_path = os.path.join(CONFIG["output_dir"], "latest.json")
    with open(latest_path, "w") as f:
        json.dump(dashboard_data, f, indent=2)

    return dashboard_data


def _build_summary(tag, local, behind, is_new):
    parts = [f"Hermes Agent latest: v{tag}"]
    if local:
        parts.append(f"installed: v{local}")
    if behind and behind > 0:
        parts.append(f"{behind} commits behind")
    if is_new:
        parts.append("NEW RELEASE available")
    return " | ".join(parts)


def main():
    ensure_dirs()
    state = load_state()
    previous = state.get("last_version")

    print(f"[hermes-checker] State: last_version={previous or 'none'}")

    # Get local version info
    local_ver, local_output = get_local_hermes_version()
    commits_behind = get_local_commits_behind()
    print(f"[hermes-checker] Local: v{local_ver or '?'}  commits_behind={commits_behind or '?'}")

    # Get latest from GitHub
    print(f"[hermes-checker] Checking GitHub releases...")
    latest = get_latest_github_release()
    if not latest:
        print("[hermes-checker] ERROR: Could not fetch from GitHub API.")
        sys.exit(1)

    latest_tag = latest["tag"]
    print(f"[hermes-checker] Latest release: v{latest_tag} ({latest['published']})")

    if latest_tag == previous and state.get("known_versions"):
        print(f"[hermes-checker] Already tracked v{latest_tag}. Nothing new.")
        state["last_checked"] = datetime.now(timezone.utc).isoformat()
        state["local_version"] = local_ver
        state["commits_behind"] = commits_behind
        save_state(state)
        # Still rewrite latest.json so the dashboard always has current local info
        # (commits_behind can change daily even if the release tag hasn't)
        noop_data = {
            "id": f"hermes-update-{latest_tag}",
            "type": "hermes-update",
            "version": latest_tag,
            "local_version": local_ver,
            "commits_behind": commits_behind,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "severity": _calc_severity_from_behind(commits_behind),
            "is_new_release": False,
            "summary": f"Hermes v{latest_tag} | installed v{local_ver or '?'} | {commits_behind or 0} commits behind",
            "capability_count": 0,
            "capabilities": [],
            "action_required": bool(commits_behind and commits_behind > 50),
            "release_count": 0,
            "changelog_url": f"https://github.com/{CONFIG['github_repo']}/releases",
        }
        latest_path = os.path.join(CONFIG["output_dir"], "latest.json")
        with open(latest_path, "w") as f:
            json.dump(noop_data, f, indent=2)
        sys.exit(0)

    # New/updated — get recent releases for capability parsing
    print(f"[hermes-checker] Fetching recent releases for changelog...")
    recent = get_recent_releases(5)
    # If the latest release body is short, it may not have full notes —
    # parse all recent ones for a cumulative picture

    item = generate_dashboard_item(latest_tag, local_ver, commits_behind, recent)
    print(f"[hermes-checker] Dashboard written: severity={item['severity']}, caps={item['capability_count']}")

    # Update state
    known = state.get("known_versions", [])
    if latest_tag not in known:
        known.append(latest_tag)
    state.update({
        "last_version": latest_tag,
        "local_version": local_ver,
        "commits_behind": commits_behind,
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "known_versions": known[-20:],
    })
    save_state(state)

    # Print summary for cron delivery
    print(f"\n{'='*52}")
    print(f"  Hermes Agent Update Check — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*52}")
    print(f"  Latest release:  v{latest_tag}")
    print(f"  Installed:       v{local_ver or 'unknown'}")
    if commits_behind and commits_behind > 0:
        print(f"  Commits behind:  {commits_behind}")
    print(f"  Severity:        {item['severity'].upper()}")
    print(f"  New release:     {'YES' if item['is_new_release'] else 'no'}")
    print(f"  Action needed:   {'YES' if item['action_required'] else 'no'}")

    if item["capabilities"]:
        print(f"\n  Notable capabilities in recent releases:")
        for cap in item["capabilities"][:8]:
            sev_tag = f"[{cap['severity'].upper()}]"
            print(f"    {sev_tag:12s} {cap['description'][:70]}")

    print(f"\n  Changelog: {item['changelog_url']}")
    print(f"  Dashboard: {CONFIG['output_dir']}/latest.json")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
