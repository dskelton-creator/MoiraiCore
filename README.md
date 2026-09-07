# 🧠 MoiraiCore — Local-First AI Orchestration Platform

**MoiraiCore** is a self-hosted, local-first agent orchestration platform. It turns scattered AI models and coding agents into a single governed workspace with a Mission Control dashboard, a memory vault, a goal engine, and a strict multi-tier code pipeline.

Everything runs on your machine. Your data never leaves it.

users can use a mix of either fully cloud models or local llm's to help maintain security and sensitivity of data on your local machine 

---

## Highlights

- **Mission Control dashboard** — a single-file SPA (`dashboard/`) that unifies agents, goals, kanban, vault, outputs, and reports.
- **3-Tier Agent Pipeline** — a governed code-generation pipeline with a ScrumMaster (Tier 1), a provider-agnostic architect engine (Tier 2), and a local execution worker (Tier 3), gated by ScrumGate before anything merges to `src/`.
- **Provider-agnostic Tier 2** — bring your own model: any OpenAI-compatible endpoint by default, Gemini native as a built-in alternative. No vendor is hardcoded.
- **Memory Vault** — markdown/SQLite-FTS5 knowledge graph agents read and write.
- **Goal Engine + Kanban** — decompose goals into tasks, route them to agents, evaluate artifacts, and synthesize reports.
- **Full isolation** — customer projects live in their own spaces; they call MoiraiCore over HTTP and never touch core config.

## Quick Start

> **Prerequisite:** MoiraiCore depends on [Hermes Agent](https://github.com/NousResearch/hermes-agent) for model execution (the Tier 1 director and task agents). Install Hermes first: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # or: pip install -r requirements.txt

# 2. Configure the Tier 2 model backend (any OpenAI-compatible endpoint)
export TIER2_BASE_URL=https://api.example.com/v1   # your OpenAI-compatible endpoint
export TIER2_MODEL=your-model-id                   # operator-selected per tier
export TIER2_API_KEY=...                           # credential for that endpoint
#   optional Gemini-native alternative:
#   export TIER2_PROVIDER=gemini
#   export GEMINI_API_KEY=...
#
# Zero-credentials smoke test: skip step 2 entirely — the pipeline falls back
# to built-in template artifacts when no backend is configured.

# 3. Create the Python 3.11 virtualenv (standard interpreter) and start
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python scripts/server.py --port 7879
# open http://localhost:7879
```

> **Note:** MoiraiCore runs on port **7879** (Hagent OS uses 7878). The command-line `hermes` binary must be on your PATH for the goal engine's Tier 1 orchestration.

### 60-second demo: the merge gate that says no

No keys, no network, no model — watch the gate refuse spec-violating work and accept a compliant retry:

```bash
.venv/bin/python scripts/demo_gate.py
```

```text
➜ 1. Decompose the goal into a spec-anchored backlog
✔ 3 tasks, each carrying an auto-derived spec contract
➜ 2. Submit a lazy artifact that ignores the acceptance criteria
    gate verdict: FAIL
✔ Merge gate REFUSED the artifact — AC1 not addressed: 'Artifact addresses the stated intent…'
➜ 3. Retry: submit an artifact that satisfies the acceptance criteria
    gate verdict: PASS
✔ Merge gate ACCEPTED the compliant artifact — task marked DONE
```

That refusal + convergence loop is the core of MoiraiCore's spec-anchored SDD.

### First-run setup (onboarding wizard)

On first launch, MoiraiCore shows a **setup wizard** that walks you through:

1. **Create your admin account** — either a local username + password, or
   "Sign in with Google". To enable Google sign-in, create an OAuth 2.0 client
   of type *Web application* in the [Google Cloud console][gcp] with an
   **Authorized redirect URI** of `http://localhost:<port>/api/auth/google/callback`,
   then paste its Client ID into the wizard (the public id is all that's
   needed — PKCE is used, no secret). The first account becomes the admin.
2. **System configuration** — instance name, workspace directory, and the
   default Tier 1 / Tier 2 / Tier 3 models that drive the agent pipeline.
3. **Done** — configuration is saved to `config/system.json` and Mission Control
   opens.

Returning users can reach the same screen later via **Settings → Setup & SSO**.
Google sign-in is available on the login screen whenever a Client ID is set.

[gcp]: https://console.cloud.google.com/apis/credentials

### Tier 2 backend configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `TIER2_PROVIDER` | `openai` | `openai` (OpenAI-compatible) or `gemini` (native) |
| `TIER2_MODEL` | — (operator sets) | Model id for the OpenAI-compatible backend |
| `TIER2_BASE_URL` | — (operator sets) | OpenAI-compatible endpoint |
| `TIER2_API_KEY` | — | Credential for the OpenAI-compatible endpoint |
| `GEMINI_API_KEY` | — | Credential (Gemini backend) |
| `TIER2_MAX_TOKENS` | `8192` | Output budget (keep generous for reasoning models) |

## 3-Tier Pipeline

```
┌────────────────────────────────────────────────────┐
│        MoiraiCore (Tier 1 — ScrumMaster)            │
│   backlog · task assignment · artifact evaluation   │
│                                                     │
│   ┌───────────────┐   ┌────────────────┐           │
│   │  TIER 2        │   │  TIER 3        │           │
│   │  Architect     │   │  Local worker  │           │
│   │  Any OpenAI-    │  │  Local models  │           │
│   │  compatible API │  │  (Ollama etc.) │           │
│   │  blueprints     │   │  micro-fixes  │           │
│   └───────┬───────┘   └───────┬────────┘           │
│           └────────┬──────────┘                    │
│                ┌───▼───────┐                       │
│                │ ScrumGate │  merge queue → eval →  │
│                │  src/     │  merge to protected    │
│                └───────────┘  src/                 │
└────────────────────────────────────────────────────┘
```

Direct writes to `src/` are blocked; all code goes through generate → evaluate → merge.

## Repository Layout

```
MoiraiCore/
├── scripts/          ← Server + orchestration modules (auth, goal engine, scrum, vault, workers)
├── dashboard/        ← Mission Control SPA
├── moirai/           ← Conversational intelligence engine
├── docs/             ← Design wiki
├── tests/            ← Test suite
├── data/             ← Architecture diagrams
├── public/           ← Static web assets
└── README.md
```

*Customer projects, personal workspace, private memory vault, and runtime state are intentionally **not** part of this public repository.*

## Tests

```bash
cd scripts
python3 test_tier2_transport.py     # Tier 2 transport + truncation/continuation
python3 test_tier_fallback.py         # Tier 2/3 failover
python3 test_scrum_master.py          # Scrum pipeline (needs pytest)
```

## Security

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md). MoiraiCore uses JWT auth (24h access / 30d refresh), PBKDF2-SHA256 password hashing, and lockout protection; secrets live in `.env`/`config/auth` and are git-ignored.

## License

[MIT](LICENSE)
