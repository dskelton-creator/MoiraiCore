"""
Agent Working Method — a concise 10x-style discipline preamble injected into
Tier 2/3 generation prompts so coding agents leave durable judgment, not just
output. Distilled from the agent-loom "10x" instruction set.

Imported best-effort by scrum_gate.py and scrum_master.py; it is a plain string
constant, so a missing import must never break the pipeline.
"""

# Keep this token-lean: Tier 3 runs on a local model with a limited context
# window. Four high-value patterns only — no ceremony.
WORKING_METHOD_PREAMBLE = """## Working Method (10x discipline)

While you work, apply this discipline:

1. **Challenge ambiguity.** If the task is vague or underspecified, state your working assumption explicitly before coding — never silently invent requirements or semantics.
2. **Record decisions.** Briefly note WHY you chose your approach (including alternatives you rejected), so a future engineer inherits the reasoning, not just the code.
3. **Evidence over vibes.** Verify your output compiles/runs before finishing; report actual results, not assumptions.
4. **Capture lessons.** End your output with a short "Decisions & Lessons" note listing any dead-ends, surprises, or reusable patterns you hit.

This method governs HOW you work, not WHAT you build — follow the task spec above."""