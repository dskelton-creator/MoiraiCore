"""llm_json.py — shared robust JSON extraction for LLM responses.

Used by the bridge consumers in both Hagent (~/agent-os/scripts) and MoiraiCore
(~/MoiraiCore/scripts). Replaces the fragile `re.search(r"\{[\s\S]*\}", resp)`
pattern with a best-effort extractor that handles the real-world failure modes:

  1. Clean JSON object (the common case)
  2. JSON inside markdown fences (```json ... ```)
  3. JSON with prose before/after ("Here is the JSON: {...}")
  4. Greedy-match pitfall: `{...} {won't match this}` — the old regex grabs from
     the FIRST `{` to the LAST `}`, which fails when trailing braces/prose
     follow the real object. We use brace-balanced scanning instead.
  5. Nested braces inside strings (e.g. code samples in "desc" fields)
  6. Trailing commas (some models emit them)

API:
    extract_json(text, expect_key=None) -> dict | None
        expect_key: if given, prefer the smallest candidate object containing
        that key (guards against wrapping-prose capturing extra braces).

    parse_subtasks(text)  -> dict with "subtasks" list, or None
    parse_verdict(text)   -> dict with score/status keys, or None
"""
from __future__ import annotations

import json
import re


def _strip_fences(text: str) -> str:
    return text.strip()


def _balanced_objects(text: str):
    """Yield candidate JSON object substrings using brace-balanced scanning
    that respects string literals (quotes and escapes)."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    i = j + 1
                    break
        else:
            break  # unbalanced to EOF — discard this opener


def _loads_lenient(s: str):
    """json.loads with trailing-comma tolerance."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", s)
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


def extract_json(text: str, expect_key: str = None):
    """Extract the best JSON object from an LLM response. Returns dict or None."""
    if not text:
        return None
    t = text.strip()

    candidates = []
    # 1. Fence-stripped whole-text attempt
    inner = t
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[-1].strip().endswith("```"):
            inner = "\n".join(lines[1:-1]).strip()
        else:
            inner = "\n".join(lines[1:]).strip()
    obj = _loads_lenient(inner)
    if isinstance(obj, dict):
        candidates.append(obj)

    # 2. Brace-balanced scan (handles prose around/between objects)
    for s in _balanced_objects(t):
        o = _loads_lenient(s)
        if isinstance(o, dict):
            candidates.append(o)
            if expect_key and expect_key in o:
                break  # smallest-first scan: first containing match wins

    if not candidates:
        return None
    if expect_key:
        for o in candidates:
            if expect_key in o:
                return o
    # No key preference (or no candidate has it): prefer a whole-text parse,
    # else the first balanced object.
    return candidates[0] if candidates else None


# ── Domain-specific parsers ──

def parse_subtasks(text):
    """Parse a goal-decomposition response. Returns the full dict (with
    'subtasks' list) or None."""
    data = extract_json(text, expect_key="subtasks")
    if data and isinstance(data.get("subtasks"), list) and data["subtasks"]:
        return data
    return None


VALID_STATUSES = ("pass", "partial", "fail")


def parse_verdict(text):
    """Parse a verification-verdict response into a normalized dict, or None.
    Normalizes status from score when absent/invalid."""
    data = extract_json(text, expect_key="score")
    if data is None:
        return None
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        return None
    status = data.get("status")
    if status not in VALID_STATUSES:
        status = "pass" if score >= 0.8 else "partial" if score >= 0.5 else "fail"
    return {
        "score": score,
        "status": status,
        "reasoning": data.get("reasoning", "No reasoning provided"),
        "suggestions": data.get("suggestions", []) or [],
        "missing_elements": data.get("missing_elements", []) or [],
    }
