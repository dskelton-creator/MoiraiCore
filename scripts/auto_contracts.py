"""Improvement #9 — Auto-derive contracts for spec-anchored SDD.

When operators don't supply contracts, the pipeline drafts them:

  1. Model path (Tier 2 backend via gemini_worker): a dedicated prompt asks
     the model to extract cross-file agreements (endpoints, units, fields,
     shapes) from the goal + task descriptions. JSON parsed defensively.
  2. Heuristic path (deterministic, always available): scan task titles/
     descriptions for route-like strings, unit words, and known field names.
  3. Merge: model overrides heuristics per section/key. Operator task-level
     contracts (spec.contracts) always win over both at merge time.

Entry points:
  - auto_contracts(goal, task_descriptions, use_model=True) -> dict
  - ScrumMaster.derive_and_apply_contracts(...) — runs at decompose time,
    persists specs/contracts.md, never blocks the pipeline.
"""
from __future__ import annotations

import json
import re


# ── heuristic extraction (deterministic, always runs) ────────────────────

def heuristic_contracts(task_descriptions: list[dict]) -> dict:
    """Extract obvious shared agreements from titles/descriptions.

    Detects:
      - API route strings  -> contracts["endpoints"][<name>]
      - unit words         -> contracts["units"]["time"]
      - known field names  -> contracts["fields"]
    """
    text = " ".join(
        str(td.get("title", "")) + " " + str(td.get("description", ""))
        for td in task_descriptions
    ).lower()

    out: dict = {}

    routes = re.findall(r"/api/[a-z0-9_\-/{}]+", text)
    if routes:
        eps = {}
        for r in sorted(set(routes)):
            name = r.rstrip("/").split("/")[-1].strip("{}") or "root"
            eps[re.sub(r"[^a-z0-9_]", "_", name)] = r
        out["endpoints"] = eps

    if re.search(r"\bseconds\b", text):
        out.setdefault("units", {})["time"] = "seconds"
    elif re.search(r"\bminutes\b", text):
        out.setdefault("units", {})["time"] = "minutes"
    elif re.search(r"hours[- ]?of[- ]?day|\bstarthour\b", text):
        out.setdefault("units", {})["time"] = "hours-of-day"

    # known field names, both JSON form ("taskId":) and prose form ("has taskId")
    known_pat = (r"(?:`?([a-z][a-z0-9_]{2,20})`?\s*:\s*|"
                 r"\b(taskid|employeeid|requestid|userid|shiftid|"
                 r"starthour|endhour)\b)")
    found = []
    for m in re.finditer(known_pat, text):
        found.append(m.group(1) or m.group(2))
    known = [f for f in dict.fromkeys(found) if f]
    if known:
        out.setdefault("fields", {})
        for f in known[:8]:
            out["fields"][f] = f

    return out


# ── model extraction ─────────────────────────────────────────────────────

def build_contract_prompt(goal: str, task_descriptions: list[dict]) -> str:
    lines = [
        "You are preparing the shared contracts for a multi-task software project.",
        f"Project goal: {goal}",
        "Tasks:",
    ]
    for td in task_descriptions:
        lines.append(f"- {td.get('title', '')}: {td.get('description', '')}")
    lines.append(
        "\nExtract ONLY agreements that must hold across files for the code to "
        "integrate: endpoint paths, units (time/duration), shared field names, "
        "data shapes. Return STRICT JSON, no prose:\n"
        '{"endpoints": {"<name>": "<path>"}, "units": {"<name>": "<unit>"}, '
        '"fields": {"<name>": "<canonical form>"}}\n'
        "Omit a section if nothing applies. Maximum 12 entries total."
    )
    return "\n".join(lines)


def parse_contract_json(raw: str) -> dict:
    """Parse the model's JSON; tolerates ```json fences and surrounding prose."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out: dict = {}
    for section in ("endpoints", "units", "fields", "shapes"):
        entries = data.get(section)
        if isinstance(entries, dict) and entries:
            out[section] = {str(k): str(v) for k, v in list(entries.items())[:12]}
    return out


def model_contracts(goal: str, task_descriptions: list[dict]) -> dict:
    """Ask the Tier 2 backend for contracts; {} on any failure."""
    try:
        from gemini_worker import GeminiConfig, is_gemini_available, _call_gemini
        cfg = GeminiConfig.from_env()
        if not is_gemini_available(cfg):
            return {}
        raw = _call_gemini(cfg, build_contract_prompt(goal, task_descriptions),
                           json_mode=True)
        if isinstance(raw, dict):
            return {s: {str(k): str(v) for k, v in e.items()}
                    for s, e in raw.items() if isinstance(e, dict)}
        return parse_contract_json(raw if isinstance(raw, str) else "")
    except Exception:
        return {}


def auto_contracts(goal: str, task_descriptions: list[dict],
                   use_model: bool = True) -> dict:
    """Heuristic + model, merged (model wins per key). Never raises."""
    try:
        merged = heuristic_contracts(task_descriptions)
    except Exception:
        merged = {}
    if use_model:
        try:
            for section, entries in model_contracts(goal, task_descriptions).items():
                merged.setdefault(section, {}).update(entries)
        except Exception:
            pass
    return merged
