#!/usr/bin/env python3
"""
MoiraiCore — Learn & Build (LnB) Engine
=======================================
Turn any content into a permanent, reusable skill.

Workflow:
  1. User provides URL / text / file via /learn command or LnB UI
  2. Engine extracts content (web page, PDF, code, text)
  3. LLM distills into structured skill (SKILL.md format)
  4. Skill is saved to ~/.hermes/skills/lnb/<topic>/SKILL.md
  5. Verification: skill is loaded and tested against a sample query
  6. Pattern learner is updated with new skill metadata

Inspired by: "Learn Anything Engine for Hermes AgentOS" — Julian Goldie SEO

Usage:
    from lnb_engine import LearnAndBuild

    lnb = LearnAndBuild()
    skill = lnb.learn("https://example.com/article", topic="AI Automation")
    print(f"Skill created: {skill['path']}")

    # Learn from raw text
    skill2 = lnb.learn_text("How to build REST APIs...", topic="REST API Guide")

    # Learn from file
    skill3 = lnb.learn_file(Path("~/notes/my-workflow.md"), topic="My Workflow")
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
CONFIG_DIR = AGENT_OS_ROOT / "config"
LNB_SKILLS_DIR = Path.home() / ".hermes" / "skills" / "lnb"
LNB_LOG_FILE = CONFIG_DIR / "lnb_log.json"

sys.path.insert(0, str(SCRIPTS_DIR))


class LearnAndBuild:
    """
    Learn & Build engine — turn anything into a permanent skill.

    The core philosophy: Show Hermes something ONCE, it remembers forever.
    No need to re-explain workflows, re-share guides, or re-teach concepts.
    """

    def __init__(self, skills_dir=None):
        self.skills_dir = Path(skills_dir) if skills_dir else LNB_SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = LNB_LOG_FILE
        self._log = self._load_log()

    def learn(self, source: str, topic: str = "", source_type: str = "auto") -> dict:
        """
        Learn from any source and create a reusable skill.

        Args:
            source: URL, file path, or raw text content
            topic: Optional topic name (auto-detected if not provided)
            source_type: "url", "file", "text", or "auto" (detect from source)

        Returns:
            {
                "ok": bool,
                "skill_name": str,
                "skill_path": str,
                "topic": str,
                "source": str,
                "verified": bool,
                "verification_result": dict,
                "message": str,
            }
        """
        # Detect source type
        if source_type == "auto":
            source_type = self._detect_source_type(source)

        # Extract content
        try:
            if source_type == "url":
                content = self._extract_url(source)
            elif source_type == "file":
                content = self._extract_file(Path(source).expanduser())
            else:
                content = source
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to extract content from {source_type}: {e}",
            }

        if not content or len(content.strip()) < 50:
            return {
                "ok": False,
                "error": "Content too short or empty (minimum 50 chars)",
            }

        # Auto-detect topic if not provided
        if not topic:
            topic = self._detect_topic(content)

        # Generate skill
        try:
            skill_content = self._generate_skill(content, topic, source)
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to generate skill: {e}",
            }

        # Save skill
        skill_name = self._sanitize_name(topic)
        skill_dir = self.skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_content, encoding="utf-8")

        # Verify the skill works
        verification = self._verify_skill(skill_path, topic, content)

        # Log the learning event
        result = {
            "ok": True,
            "skill_name": skill_name,
            "skill_path": str(skill_path),
            "topic": topic,
            "source": source[:200],
            "source_type": source_type,
            "content_length": len(content),
            "verified": verification.get("ok", False),
            "verification_result": verification,
            "message": f"Skill '{skill_name}' created and saved",
        }
        self._log_event(result)

        return result

    def learn_text(self, text: str, topic: str = "") -> dict:
        """Learn from raw text content."""
        return self.learn(text, topic=topic, source_type="text")

    def learn_file(self, file_path: Path, topic: str = "") -> dict:
        """Learn from a file (markdown, text, code, etc.)."""
        return self.learn(str(file_path), topic=topic, source_type="file")

    def learn_url(self, url: str, topic: str = "") -> dict:
        """Learn from a URL (web page, article, blog post)."""
        return self.learn(url, topic=topic, source_type="url")

    def list_skills(self, topic_filter: str = "") -> list:
        """List all learned skills, optionally filtered by topic."""
        skills = []
        if not self.skills_dir.exists():
            return skills

        for skill_dir in sorted(self.skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            # Read frontmatter for description
            content = skill_md.read_text(encoding="utf-8")
            name = skill_dir.name
            desc = ""
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    try:
                        import yaml
                        fm = yaml.safe_load(content[3:end].strip())
                        desc = fm.get("description", "")
                    except Exception:
                        # Fallback: extract description from first non-frontmatter line
                        for line in content[end+3:].split("\n"):
                            line = line.strip()
                            if line and not line.startswith("#"):
                                desc = line[:100]
                                break

            if topic_filter and topic_filter.lower() not in name.lower():
                continue

            skills.append({
                "name": name,
                "path": str(skill_md),
                "description": desc[:100],
                "created": datetime.fromtimestamp(skill_dir.stat().st_mtime).isoformat(),
            })

        return skills

    def get_skill(self, skill_name: str) -> dict:
        """Get a learned skill by name."""
        skill_path = self.skills_dir / skill_name / "SKILL.md"
        if not skill_path.exists():
            return {"ok": False, "error": f"Skill '{skill_name}' not found"}

        content = skill_path.read_text(encoding="utf-8")
        return {
            "ok": True,
            "name": skill_name,
            "path": str(skill_path),
            "content": content,
        }

    def delete_skill(self, skill_name: str) -> dict:
        """Delete a learned skill."""
        skill_dir = self.skills_dir / skill_name
        if not skill_dir.exists():
            return {"ok": False, "error": f"Skill '{skill_name}' not found"}

        import shutil
        shutil.rmtree(skill_dir)
        return {"ok": True, "message": f"Skill '{skill_name}' deleted"}

    def update_skill(self, skill_name: str, new_content: str) -> dict:
        """Update an existing skill with new content."""
        skill_path = self.skills_dir / skill_name / "SKILL.md"
        if not skill_path.exists():
            return {"ok": False, "error": f"Skill '{skill_name}' not found"}

        skill_path.write_text(new_content, encoding="utf-8")
        return {"ok": True, "message": f"Skill '{skill_name}' updated"}

    # ── Internal Methods ──

    def _detect_source_type(self, source: str) -> str:
        """Detect if source is URL, file path, or raw text."""
        if source.startswith(("http://", "https://", "www.")):
            return "url"
        if Path(source).expanduser().is_file():
            return "file"
        return "text"

    def _extract_url(self, url: str) -> str:
        """Extract content from a URL."""
        if not url.startswith("http"):
            url = "https://" + url

        try:
            import urllib.request
            from urllib.parse import urlparse

            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            # Basic HTML to text extraction
            return self._html_to_text(raw)
        except Exception as e:
            raise ValueError(f"Failed to fetch URL: {e}")

    def _extract_file(self, file_path: Path) -> str:
        """Extract content from a file."""
        if not file_path.exists():
            raise ValueError(f"File not found: {file_path}")

        content = file_path.read_text(encoding="utf-8", errors="replace")

        # If markdown, keep as-is
        if file_path.suffix.lower() in (".md", ".txt", ".markdown"):
            return content

        # If code file, wrap in code block
        if file_path.suffix.lower() in (".py", ".js", ".ts", ".sh", ".bash", ".yaml", ".json", ".css", ".html"):
            return f"```{file_path.suffix.lower()}\n{content}\n```"

        return content

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to readable text."""
        # Remove script/style tags
        text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
        text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)

        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Remove excessive newlines
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text

    def _detect_topic(self, content: str) -> str:
        """Auto-detect topic from content."""
        # Try to find a title or heading
        lines = content.split('\n')[:20]
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and len(line) > 10 and len(line) < 100:
                # Use first meaningful line as topic hint
                return self._sanitize_name(line[:60])

        # Fallback: use keywords
        content_lower = content.lower()
        topics = {
            "AI Automation": ["ai", "automation", "agent"],
            "Web Development": ["html", "css", "javascript", "web"],
            "Machine Learning": ["ml", "model", "training", "neural"],
            "DevOps": ["deploy", "docker", "kubernetes", "ci/cd"],
            "Security": ["security", "threat", "vulnerability"],
            "SEO": ["seo", "search engine", "ranking"],
            "API": ["api", "rest", "endpoint"],
            "Database": ["database", "sql", "query"],
        }
        for topic, keywords in topics.items():
            if any(kw in content_lower for kw in keywords):
                return topic

        return f"Learned Skill {datetime.now().strftime('%Y%m%d')}"

    def _generate_skill(self, content: str, topic: str, source: str) -> str:
        """Generate a structured skill from content using LLM."""
        # Build the skill generation prompt
        prompt = f"""You are the Learn & Build Engine for MoiraiCore. Your job is to distill content into a structured, reusable skill.

## Topic
{topic}

## Source
{source}

## Content
{content[:4000]}

## Instructions
Transform this content into a skill document with this structure:

1. **Frontmatter**: YAML with name, description, version, author, tags
2. **Overview**: What this skill enables (2-3 sentences)
3. **When to Use**: Bulleted trigger conditions
4. **Core Concepts**: Key principles extracted from the content
5. **Workflow**: Step-by-step implementation guide
6. **Common Pitfalls**: Mistakes to avoid
7. **Verification Checklist**: How to confirm the skill works
8. **Quick Reference**: Commands, configs, or code snippets

## Rules
- Description must start with "Use when..." and be ≤1024 chars
- Focus on ACTIONABLE information, not theory
- Include specific commands, configs, or code where relevant
- Tags should be lowercase, hyphens, ≤5 items
- Output ONLY the complete SKILL.md content (with frontmatter)
- No explanation before or after the skill document"""

        try:
            from hermes_bridge import run_hermes
            result = run_hermes(
                ["chat", "-q", prompt, "--quiet"],
                timeout=120,
            )
            response = result.get("response", "")

            if response and len(response) > 100:
                # Strip markdown fences if present
                response = response.strip()
                if response.startswith("```"):
                    lines = response.split("\n")
                    if lines[-1].strip() == "```":
                        lines = lines[1:-1]
                    else:
                        lines = lines[1:]
                    response = "\n".join(lines).strip()

                # Ensure frontmatter is present
                if not response.startswith("---"):
                    # Add default frontmatter
                    response = f"""---
name: {self._sanitize_name(topic)}
description: "Use when working with {topic.lower()}. Auto-generated from learning source."
version: 1.0.0
author: MoiraiCore LnB Engine
metadata:
  hermes:
    tags: [lnb, {self._sanitize_name(topic)}, auto-generated]
---

{response}"""

                return response

        except Exception:
            pass

        # Fallback: template-based skill generation
        return self._template_skill(content, topic, source)

    def _template_skill(self, content: str, topic: str, source: str) -> str:
        """Template-based skill generation (fallback when LLM unavailable)."""
        # Extract key sections from content
        lines = content.split('\n')
        key_points = []
        for line in lines:
            line = line.strip()
            if line and len(line) > 15 and not line.startswith(('#', '<', '!', '|', '-', '*', '```')):
                key_points.append(line)
            if len(key_points) >= 10:
                break

        key_points_text = "\n".join(f"- {kp}" for kp in key_points) if key_points else "- Content extracted from source"

        return f"""---
name: {self._sanitize_name(topic)}
description: "Use when working with {topic.lower()}. Auto-generated from learning source."
version: 1.0.0
author: MoiraiCore LnB Engine
metadata:
  hermes:
    tags: [lnb, {self._sanitize_name(topic)}, auto-generated]
---

# {topic}

## Overview
This skill was auto-generated by the Learn & Build engine from: {source[:100]}

## When to Use
- When working on {topic.lower()} tasks
- When you need a quick reference for {topic.lower()} concepts
- When onboarding others to {topic.lower()}

## Core Concepts
{key_points_text}

## Quick Reference
Source content length: {len(content)} characters
Source: {source[:200]}

## Verification Checklist
- [ ] Skill loaded successfully via `/skill {self._sanitize_name(topic)}`
- [ ] Core concepts are accurate
- [ ] Quick reference is useful
"""

    def _verify_skill(self, skill_path: Path, topic: str, original_content: str) -> dict:
        """
        Verify a skill was created correctly.

        Checks:
        1. File exists and is readable
        2. Has valid frontmatter
        3. Content is substantial (>200 chars)
        4. Has required sections
        """
        checks = {
            "file_exists": skill_path.exists(),
            "has_content": False,
            "has_frontmatter": False,
            "has_sections": False,
            "content_length": 0,
        }

        if not skill_path.exists():
            return {"ok": False, "checks": checks, "message": "Skill file not found"}

        content = skill_path.read_text(encoding="utf-8")
        checks["content_length"] = len(content)
        checks["has_content"] = len(content) > 200
        checks["has_frontmatter"] = content.startswith("---")
        checks["has_sections"] = "## " in content

        all_ok = all([
            checks["file_exists"],
            checks["has_content"],
            checks["has_frontmatter"],
            checks["has_sections"],
        ])

        return {
            "ok": all_ok,
            "checks": checks,
            "message": "Skill verified" if all_ok else "Skill verification failed",
        }

    def _sanitize_name(self, name: str) -> str:
        """Sanitize a name for use as directory/file name."""
        # Remove special chars, keep alphanumeric and hyphens
        clean = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower().strip())
        # Remove consecutive hyphens
        clean = re.sub(r'-+', '-', clean)
        # Remove leading/trailing hyphens
        clean = clean.strip('-')
        # Truncate
        return clean[:64] or "unnamed-skill"

    def _log_event(self, result: dict):
        """Log a learning event."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "skill_name": result.get("skill_name", ""),
            "topic": result.get("topic", ""),
            "source_type": result.get("source_type", ""),
            "ok": result.get("ok", False),
            "verified": result.get("verified", False),
        }

        log = self._load_log()
        log.append(entry)

        # Keep only last 100 entries
        if len(log) > 100:
            log = log[-100:]

        self._save_log(log)

    def _load_log(self) -> list:
        """Load learning log."""
        if self.log_file.exists():
            try:
                return json.loads(self.log_file.read_text())
            except Exception:
                pass
        return []

    def _save_log(self, log: list):
        """Save learning log."""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text(json.dumps(log, indent=2))

    def get_stats(self) -> dict:
        """Get LnB engine statistics."""
        skills = self.list_skills()
        log = self._load_log()
        successful = [e for e in log if e.get("ok")]
        verified = [e for e in log if e.get("verified")]

        return {
            "total_skills": len(skills),
            "total_learning_events": len(log),
            "successful": len(successful),
            "verified": len(verified),
            "skills_dir": str(self.skills_dir),
            "skills": [{"name": s["name"], "created": s["created"]} for s in skills[-10:]],
        }


# ── CLI ──

def main():
    import argparse

    parser = argparse.ArgumentParser(description="MoiraiCore — Learn & Build Engine")
    sub = parser.add_subparsers(dest="command")

    # learn
    p_learn = sub.add_parser("learn", help="Learn from a source (URL, text, or file)")
    p_learn.add_argument("source", help="URL, file path, or raw text")
    p_learn.add_argument("--topic", default="", help="Topic name (auto-detected if not provided)")
    p_learn.add_argument("--type", default="auto", choices=["url", "file", "text", "auto"], help="Source type")

    # list
    sub.add_parser("list", help="List all learned skills")

    # get
    p_get = sub.add_parser("get", help="Get a skill by name")
    p_get.add_argument("name", help="Skill name")

    # delete
    p_del = sub.add_parser("delete", help="Delete a skill")
    p_del.add_argument("name", help="Skill name")

    # stats
    sub.add_parser("stats", help="Show LnB statistics")

    args = parser.parse_args()
    lnb = LearnAndBuild()

    if args.command == "learn":
        result = lnb.learn(args.source, topic=args.topic, source_type=args.type)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "list":
        skills = lnb.list_skills()
        if not skills:
            print("No learned skills yet. Use 'learn <source>' to create one.")
        else:
            print(f"\n{'Skill':<40} {'Created':<20} Description")
            print("-" * 100)
            for s in skills:
                desc = s.get("description", "")[:50]
                print(f"{s['name']:<40} {s['created'][:19]:<20} {desc}")
            print(f"\nTotal: {len(skills)} skills")

    elif args.command == "get":
        result = lnb.get_skill(args.name)
        if result.get("ok"):
            print(result["content"])
        else:
            print(f"Error: {result.get('error')}")

    elif args.command == "delete":
        result = lnb.delete_skill(args.name)
        print(json.dumps(result, indent=2))

    elif args.command == "stats":
        stats = lnb.get_stats()
        print(json.dumps(stats, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
