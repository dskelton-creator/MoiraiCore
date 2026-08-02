#!/usr/bin/env python3
"""
MoiraiCore — Vault Auto-Backup
Commits the vault and config to git when meaningful changes are detected.

Usage:
    python3 vault_backup.py              # Check for changes, commit if found
    python3 vault_backup.py --force      # Force commit even if no changes
    python3 vault_backup.py --status     # Show backup status (last commit, etc.)
    python3 vault_backup.py --setup-cron # Install daily cron job at 11pm

Exit codes:
    0 = success (committed or nothing to commit)
    1 = error
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
REMOTE_URL = None  # Set to a remote URL if/when you add one (e.g., GitHub private repo)


def run(cmd: list[str], cwd=AGENT_OS_ROOT, check=False) -> subprocess.CompletedProcess:
    """Run a shell command."""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )


def has_changes() -> bool:
    """Check if there are uncommitted changes in the vault or config."""
    result = run(["git", "status", "--porcelain"])
    if result.returncode != 0:
        return False
    # Only count changes to vault/ and config/ and scripts/
    relevant = [
        line for line in result.stdout.strip().split("\n")
        if line and any(
            p in line for p in
            ["memory-vault/", "config/", "dashboard/", "scripts/", "workspace/", ".gitignore"]
        )
    ]
    return len(relevant) > 0


def get_change_summary() -> str:
    """Get a human-readable summary of what changed."""
    result = run(["git", "status", "--porcelain"])
    if not result.stdout.strip():
        return "no changes"
    lines = [l for l in result.stdout.strip().split("\n") if l]
    added = sum(1 for l in lines if l.startswith("A ") or l.startswith("??"))
    modified = sum(1 for l in lines if l.startswith("M ") or l.startswith(" M"))
    deleted = sum(1 for l in lines if l.startswith("D ") or l.startswith(" D"))
    parts = []
    if added:
        parts.append(f"{added} added")
    if modified:
        parts.append(f"{modified} modified")
    if deleted:
        parts.append(f"{deleted} deleted")
    return ", ".join(parts) or f"{len(lines)} files changed"


def do_backup(force: bool = False) -> dict:
    """Perform the actual backup."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Check for changes (unless forced)
    if not force and not has_changes():
        return {"ok": True, "committed": False, "reason": "No changes to commit"}

    summary = get_change_summary()

    # Stage relevant files
    stages = ["memory-vault/", "config/", "dashboard/", "scripts/", "workspace/", ".gitignore"]
    for stage_path in stages:
        run(["git", "add", stage_path])

    # Build commit msg
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"Auto-backup {ts} — {summary}"

    # Commit
    commit = run(["git", "commit", "-m", msg])
    if commit.returncode != 0:
        # Try to give a useful error
        error = commit.stderr.strip() or commit.stdout.strip() or "Unknown git error"
        return {"ok": False, "committed": False, "error": error}

    # Push if remote configured
    pushed = False
    if REMOTE_URL:
        push = run(["git", "push"])
        pushed = push.returncode == 0

    commit_hash = run(["git", "log", "--oneline", "-1"]).stdout.strip()[:20]

    return {
        "ok": True,
        "committed": True,
        "summary": summary,
        "commit": commit_hash,
        "message": msg,
        "pushed": pushed,
        "timestamp": now,
    }


def get_status() -> dict:
    """Get backup repository status."""
    # Check if git repo exists
    result = run(["git", "status"])
    if result.returncode != 0:
        return {"ok": False, "error": "Not a git repository or git not available"}

    # Last commit
    last = run(["git", "log", "--format=%H|%ai|%s", "-1"])
    last_commit = None
    last_date = None
    last_msg = None
    if last.returncode == 0 and last.stdout.strip():
        parts = last.stdout.strip().split("|", 2)
        if len(parts) == 3:
            last_commit = parts[0][:12]
            last_date = parts[1]
            last_msg = parts[2]

    # Total commits
    count = run(["git", "rev-list", "--count", "HEAD"])
    total_commits = count.stdout.strip() if count.returncode == 0 else "?"

    # Current changes
    status = run(["git", "status", "--porcelain"])
    pending = len([l for l in status.stdout.strip().split("\n") if l]) if status.returncode == 0 else 0

    # Remote status
    remote = run(["git", "remote", "-v"])
    has_remote = remote.returncode == 0 and bool(remote.stdout.strip())

    return {
        "ok": True,
        "total_commits": total_commits,
        "last_commit": last_commit,
        "last_date": last_date,
        "last_message": last_msg,
        "pending_changes": pending,
        "has_remote": has_remote,
        "vault_files": len(list((AGENT_OS_ROOT / "memory-vault").rglob("*.md"))),
    }


def setup_cron() -> dict:
    """Install the daily cron job."""
    cron_expr = "0 23 * * *"
    cmd = f"cd {AGENT_OS_ROOT} && python3 scripts/vault_backup.py >> {AGENT_OS_ROOT}/config/backup.log 2>&1"
    full_cron = f'{cron_expr} {cmd}'

    # Get existing crontab
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, timeout=10
    )
    existing = result.stdout if result.returncode == 0 else ""

    # Check if already installed
    if "vault_backup.py" in existing:
        return {"ok": True, "already_installed": True, "message": "Cron job already exists"}

    # Add to crontab
    new_crontab = existing.rstrip() + "\n" + full_cron + "\n"
    proc = subprocess.run(
        ["crontab", "-"], input=new_crontab, text=True, capture_output=True, timeout=10
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}

    return {
        "ok": True,
        "installed": True,
        "schedule": "Daily at 11:00 PM",
        "command": cmd,
    }


def main():
    parser = argparse.ArgumentParser(description="MoiraiCore Vault Auto-Backup")
    parser.add_argument("--force", action="store_true", help="Force commit even without changes")
    parser.add_argument("--status", action="store_true", help="Show backup status")
    parser.add_argument("--setup-cron", action="store_true", help="Install daily cron job")
    args = parser.parse_args()

    if args.setup_cron:
        result = setup_cron()
    elif args.status:
        result = get_status()
    else:
        result = do_backup(force=args.force)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    import json  # lazy import for --help speed
    main()
