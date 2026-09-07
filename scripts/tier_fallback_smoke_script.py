#!/usr/bin/env python3
"""
Test tier_fallback degradation logic against mocked engine availability.
Run: python3 scripts/test_tier_fallback.py
Exit 0 = all invariants hold.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier_fallback as tf

results = []


def case(label, gemini_up, ollama_up, preferred, chain_env, expect_engine, expect_available):
    # Patch the probe functions directly.
    tf._PROBES["gemini"] = lambda: gemini_up
    tf._PROBES["ollama"] = lambda: ollama_up
    if chain_env is not None:
        os.environ["MOIRAI_TIER_FALLBACK"] = chain_env
    else:
        os.environ.pop("MOIRAI_TIER_FALLBACK", None)

    choice = tf.resolve_tier(preferred_tier=preferred)
    ok = (choice.engine == expect_engine) and (choice.available == expect_available)
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}")
    print(f"        gemini={gemini_up} ollama={ollama_up} pref={preferred} "
          f"chain={chain_env or 'default'} -> engine='{choice.engine}' "
          f"avail={choice.available} (expected '{expect_engine}'/{expect_available})")
    if not ok:
        print(f"        reason: {choice.reason}")
    results.append(ok)


# --- Core degradation invariants ---
case("Gemini up: prefer T2 -> gemini",
     True, True, 2, None, "gemini", True)
case("Gemini DOWN, Ollama up: T2 request degrades to ollama",
     False, True, 2, None, "ollama", True)
case("Both down: clear failure, not a crash",
     False, False, 2, None, "", False)
case("T3 request with both up -> ollama (preferred tier leads)",
     True, True, 3, None, "ollama", True)
case("T3 request, ollama DOWN, gemini up -> degrades UP to gemini",
     True, False, 3, None, "gemini", True)
case("No preference, default chain, gemini up -> gemini first",
     True, True, None, None, "gemini", True)
# --- Config override ---
case("Env chain reversed (ollama,gemini): ollama leads",
     True, True, None, "ollama,gemini", "ollama", True)
case("Env chain ollama-only, ollama down -> unavailable",
     True, False, None, "ollama", "", False)

passed = sum(results)
failed = len(results) - passed
print(f"\n=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
