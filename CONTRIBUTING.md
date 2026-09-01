# Contributing to MoiraiCore

Thanks for your interest in contributing! This is a local-first agent
orchestration platform. Here's how to help.

## Ground Rules

- **Keep it local-first.** MoiraiCore runs on your machine; avoid designs that
  require cloud services or that leak user data off-device.
- **Don't bypass the pipeline.** If you touch code generation, route it through
  the 3-tier pipeline (Tier 1 ScrumMaster → Tier 2 architect → Tier 3 worker →
  ScrumGate) rather than writing directly to `src/`.
- **Secrets stay out of git.** Never commit `.env`, `config/auth`, or real API
  keys. Use env vars.

## Getting Started

```bash
git clone <your-fork>
cd MoiraiCore
pip install -r requirements.txt
```

The server needs a Tier 2 model backend. The default is OpenAI-compatible via
OpenRouter (`OPENROUTER_API_KEY`); set `TIER2_PROVIDER=gemini` + `GEMINI_API_KEY`
for the native Gemini path. See [README](README.md#tier-2-backend-configuration).

## Development Workflow

1. Fork and create a feature branch.
2. Make focused changes with tests where practical.
3. Run the relevant tests:
   ```bash
   cd scripts
   python3 test_tier2_transport.py
   python3 test_tier_fallback.py
   ```
4. Run a syntax check on changed Python files:
   ```bash
   python3 -m py_compile scripts/<changed>.py
   ```
5. Open a pull request describing the change, why it's useful, and how you
   verified it.

## Code Style

- Python 3.9+ compatible annotations (avoid `dict | None`; use `Optional`).
- Import standard library modules at module level only.
- Keep the single-file dashboard JS free of template-literal pitfalls — use
  string concatenation and inline handlers consistently.
- Prefer small, focused patches over large rewrites.

## Reporting Bugs

Open an issue with:

- The command/API you ran
- The expected vs. actual output
- Relevant logs (redact any secrets)

## Questions

Open a discussion or an issue. Be kind and specific.
