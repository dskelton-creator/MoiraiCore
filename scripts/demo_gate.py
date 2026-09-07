#!/usr/bin/env python3
"""MoiraiCore 5-minute demo — zero credentials, zero network.

Shows the core promise of MoiraiCore: a Scrum pipeline whose merge gate
*refuses* work that doesn't meet its spec, then accepts a compliant artifact
after a retry. Runs entirely on built-in template fallbacks — no API keys,
no Ollama, no Hermes required.

Usage:
    python3 scripts/demo_gate.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Strict no-credentials environment for the demo (operator env can't leak in)
for k in list(os.environ):
    if any(s in k for s in ("API_KEY", "TIER2", "TIER1", "GEMINI", "OPENAI")):
        os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrum_master import ScrumMaster, Tier  # noqa: E402

STEP = "  "
ok = lambda s: print(f"\033[32m✔\033[0m {s}")
info = lambda s: print(f"\033[36m➜\033[0m {s}")


def main() -> int:
    space = tempfile.mkdtemp(prefix="moirai_demo_")
    sm = ScrumMaster("gate-demo", project_space=space)
    sm.goal = "Build a temperature-converter module with tests"

    print("╭──────────────────────────────────────────────────╮")
    print("│  MoiraiCore demo — the merge gate that says no   │")
    print("│  (zero credentials, zero network)                │")
    print("╰──────────────────────────────────────────────────╯\n")

    # 1. Decompose
    info("1. Decompose the goal into a spec-anchored backlog")
    tasks = sm.decompose_backlog()
    for t in tasks:
        print(f"{STEP}• {t.id}: {t.title} (tier {t.tier.value})")
    ok(f"{len(tasks)} tasks, each carrying an auto-derived spec contract")
    task = tasks[0]
    task.tier = Tier.TIER_2_ARCHITECT  # template-fallback path — no model needed
    acs = task.spec.get("acceptance_criteria", [])
    print(f"{STEP}  spec for {task.id}: intent='{task.spec.get('intent', '')[:60]}...'")
    for ac in acs[:4]:
        print(f"{STEP}  acceptance criterion: {ac}")

    # 2. Submit a deliberately non-compliant artifact
    info("2. Submit a lazy artifact that ignores the acceptance criteria")
    bad = sm.submit_artifact(
        task.id,
        "implementation_plan",
        {
            "approach": "Just wing it.",
            "steps": ["do stuff"],
            # deliberately missing: deliverables, tests, acceptance criteria
        },
    )
    result = sm.evaluate_task(task.id)
    verdict = result.get("result", "?")
    print(f"{STEP}  gate verdict: {verdict}")
    if verdict == "FAIL":
        ok(f"Merge gate REFUSED the artifact — reason: {result.get('reason', '')[:80]}")
        if task.evaluation_notes:
            print(f"{STEP}  evaluation notes: {task.evaluation_notes[:140]}")
    else:
        print(f"{STEP}  (gate passed — demo continues anyway)")

    # 3. Retry with a compliant artifact addressing the spec
    info("3. Retry: submit an artifact that satisfies the acceptance criteria")
    # The production retry loop clears failed artifacts before regenerating
    # (scrum_master.py:1047) — only the latest attempt is evaluated.
    task.artifacts.clear()

    # A real Tier 2 model reads the spec and mirrors its language back into the
    # plan. We simulate exactly that: restate the intent + criteria verbatim.
    good = {
        "approach": task.spec.get("intent", "Implement the module"),
        "steps": [
            "Create converter.py with c_to_f() and f_to_c() functions",
            "Create test_converter.py covering freezing/boiling points and round-trips",
            "Run the tests and attach the passing log",
        ],
        "deliverables": ["converter.py (src/converter.py)", "test_converter.py (test function names)"],
        "files": ["src/converter.py", "src/test_converter.py"],
        "acceptance_criteria": acs,
        "tests": "pytest test_converter.py -q — all cases pass",
    }
    sm.submit_artifact(task.id, "implementation_plan", good)
    result = sm.evaluate_task(task.id)
    verdict = result.get("result", "?")
    print(f"{STEP}  gate verdict: {verdict}")
    if verdict == "PASS":
        ok("Merge gate ACCEPTED the compliant artifact — task marked DONE")
    else:
        # Template evaluators vary; report honestly
        print(f"{STEP}  gate result: {verdict} — {str(result.get('reason', ''))[:90]}")

    # 4. Wrap up
    report = sm.status_report()
    info("4. Status report")
    print(f"{STEP}{json.dumps({k: report[k] for k in report if k in ('total_tasks', 'completed', 'completion_pct')})}")
    print()
    print("In production, Tier 2 generates these artifacts with YOUR model via an")
    print("OpenAI-compatible endpoint or Gemini — the gate enforces the spec either way.")
    print("Configure it with TIER2_BASE_URL / TIER2_MODEL / TIER2_API_KEY (see README).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
