"""
tier_fallback — Single source of truth for MoiraiCore tier degradation.

BEFORE: the "Gemini T2 -> Ollama T3" degradation was implied but never
actually automatic. scrum_gate.execute_task() rigidly bound engine to tier
(`if engine=="gemini" and tier==2 / elif engine=="ollama" and tier==3 /
else error`) — so if Gemini was down at T2, the task ERRORED instead of
degrading. Availability checks (is_gemini_available / is_ollama_available)
existed in separate worker modules but were never chained. Routing logic
was scattered across hermes_bridge.py, scrum_gate.py, and model_router.py.

AFTER: one declarative chain, resolved by chaining the EXISTING availability
probes. Callers ask "give me the best engine for this task" and get back a
concrete (tier, engine) pair, or a clear reason why nothing is available.

Config precedence (first found wins):
  1. sandbox.json -> execution_workers.tier_fallback (per-project override)
  2. env MOIRAI_TIER_FALLBACK (comma-separated, e.g. "gemini,ollama")
  3. DEFAULT_CHAIN below

Each chain entry names an engine; the resolver probes them in order and
returns the first that is actually reachable right now.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("moiraicore.tier_fallback")

# Ordered degradation chain. Gemini (T2, fast, API) preferred; Ollama (T3,
# local, slower but always-on) is the resilient floor.
DEFAULT_CHAIN = ["gemini", "ollama"]

# Engine -> the tier number it runs as (mirrors scrum_gate's contract).
ENGINE_TIER = {
    "gemini": 2,
    "ollama": 3,
}


@dataclass
class TierChoice:
    """Result of resolving the fallback chain."""
    engine: str          # "gemini" | "ollama" | ""
    tier: int            # 2 | 3 | 0
    available: bool      # True if a reachable engine was found
    reason: str = ""     # human-readable explanation
    probed: tuple = ()   # engines probed, in order, for observability

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "tier": self.tier,
            "available": self.available,
            "reason": self.reason,
            "probed": list(self.probed),
        }


def _probe_gemini() -> bool:
    try:
        from gemini_worker import GeminiConfig, is_gemini_available
        return is_gemini_available(GeminiConfig.from_env())
    except Exception as e:
        logger.debug("gemini probe failed: %s", e)
        return False


def _probe_ollama(base_url: str = "http://localhost:11434") -> bool:
    try:
        from ollama_worker import is_ollama_available
        return is_ollama_available(base_url)
    except Exception as e:
        logger.debug("ollama probe failed: %s", e)
        return False


# Engine -> probe function. Extend here to add engines (e.g. antigravity).
_PROBES: dict[str, Callable[[], bool]] = {
    "gemini": _probe_gemini,
    "ollama": _probe_ollama,
}


def resolve_chain(sandbox_config: Optional[dict] = None) -> list[str]:
    """Return the ordered fallback chain from config, env, or default."""
    # 1. Per-project sandbox.json override
    if sandbox_config:
        ew = sandbox_config.get("execution_workers", {})
        chain = ew.get("tier_fallback")
        if isinstance(chain, list) and chain:
            return [str(e).strip().lower() for e in chain if str(e).strip()]
    # 2. Env override
    env_chain = os.environ.get("MOIRAI_TIER_FALLBACK", "").strip()
    if env_chain:
        return [e.strip().lower() for e in env_chain.split(",") if e.strip()]
    # 3. Built-in default
    return list(DEFAULT_CHAIN)


def resolve_tier(
    preferred_tier: Optional[int] = None,
    sandbox_config: Optional[dict] = None,
) -> TierChoice:
    """
    Resolve the best available engine.

    If preferred_tier is given (2 or 3), the chain is reordered so the engine
    matching that tier is tried first, then the rest of the chain as fallback.
    Otherwise the configured chain order is used as-is.

    Returns a TierChoice; check .available before using .engine / .tier.
    """
    chain = resolve_chain(sandbox_config)

    # Reorder so the preferred tier's engine leads, keeping the rest as fallback.
    if preferred_tier in (2, 3):
        pref_engines = [e for e in chain if ENGINE_TIER.get(e) == preferred_tier]
        rest = [e for e in chain if e not in pref_engines]
        chain = pref_engines + rest

    probed: list[str] = []
    for engine in chain:
        probe = _PROBES.get(engine)
        if probe is None:
            logger.warning("tier_fallback: unknown engine '%s' in chain, skipping", engine)
            continue
        probed.append(engine)
        if probe():
            return TierChoice(
                engine=engine,
                tier=ENGINE_TIER.get(engine, 0),
                available=True,
                reason=f"'{engine}' reachable (chain: {'->'.join(chain)})",
                probed=tuple(probed),
            )
        logger.info("tier_fallback: '%s' unavailable, degrading", engine)

    return TierChoice(
        engine="",
        tier=0,
        available=False,
        reason=f"No engine in chain reachable (probed: {'->'.join(probed) or 'none'})",
        probed=tuple(probed),
    )


if __name__ == "__main__":
    # Quick manual probe: python3 tier_fallback.py
    logging.basicConfig(level=logging.INFO)
    for pref in (None, 2, 3):
        choice = resolve_tier(preferred_tier=pref)
        print(f"preferred={pref}: {choice.to_dict()}")
