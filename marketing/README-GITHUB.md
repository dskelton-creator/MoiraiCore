# 🧵 MoiraiCore

### The local-first AI orchestration platform where **specs are contracts, not suggestions**

**MoiraiCore** turns AI models and coding agents into one governed engineering team — with a Scrum pipeline that *refuses to merge code that doesn't meet its spec*, a Mission Control dashboard, a persistent memory vault, and a 3-tier pipeline that keeps your strongest model working on architecture while your cheapest one does the legwork.

You choose the models. Everything runs on your machine. Your data never leaves it.

> Formerly "Hagent OS" — the open-source platform core, extracted and released.

[⬇ Quick Start](#-quick-start) · [🏗 Architecture](#-the-3-tier-pipeline) · [🔐 Security](#-security) · [📜 License](#-license)

---

## Why MoiraiCore?

Spec-driven development went mainstream in 2026 — but the existing tools fall into two camps:

- **Bolt-on frameworks** (Spec Kit, OpenSpec, BMAD) that rely on an external agent and *ask* the model to follow the spec.
- **Cloud platforms** (Kiro, Tessl, Cosmos) that lock you into their models and hold your code on someone else's servers.

MoiraiCore is the missing third option:

| | Bolt-on SDD frameworks | Cloud SDD platforms | **MoiraiCore** |
|---|---|---|---|
| Spec enforcement | Advisory — the agent is *told* | Varies | **Gated — artifacts that fail their spec's acceptance criteria don't merge** |
| Model choice | Whatever the external agent uses | Vendor-locked | **Bring your own — any OpenAI-compatible API, any local runtime, per tier** |
| Hosting | Depends on external agent | Vendor cloud | **Fully self-hosted, local-first** |
| Persistent project memory | Repo files | Vendor cloud | **Built-in Memory Vault (Markdown + SQLite FTS5)** |
| Governance | None | Vendor terms | **ScrumGate merge queue, audit log, JWT auth** |

**Specs are contracts.** Every task carries a machine-checkable spec — intent, constraints, acceptance criteria, out-of-scope. Generation prompts embed it; evaluation verifies against it; the merge queue enforces it. No spec, no merge.

---

## Highlights

- 🏛 **ScrumGate pipeline** — direct writes to `src/` are blocked. All code flows through generate → evaluate → merge queue. The model proposes; the gate disposes.
- 🧠 **3-Tier Agent Pipeline** — a ScrumMaster director (Tier 1), an architect engine (Tier 2), and a local execution worker (Tier 3). Assign a model to each tier independently.
- 🔌 **Bring your own models** — any OpenAI-compatible endpoint (cloud or self-hosted) or native Gemini, configured per tier. Cloud, hybrid, or 100% local — your call.
- 📋 **Goal Engine + Kanban** — decompose goals into tasks, route them to agents, evaluate artifacts, synthesize reports.
- 🗄 **Memory Vault** — a Markdown/SQLite-FTS5 knowledge graph your agents read *and* write. Learnings survive sessions.
- 🖥 **Mission Control** — a single-file SPA dashboard unifying agents, goals, kanban, vault, outputs, and reports.
- 📦 **Full project isolation** — customer projects live in their own spaces, call MoiraiCore over HTTP, and never touch core config.
- 🔐 **Real auth** — JWT (24h access / 30d refresh), PBKDF2-SHA256 hashing, lockout protection, audit log, and optional Google SSO (OAuth 2.0 + PKCE).

---

## Quick Start

> **Prerequisite:** MoiraiCore uses [Hermes Agent](https://github.com/NousResearch/hermes-agent) for Tier 1 model execution. Install it first:
> `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`

```bash
# 1. Create a Python 3.11 virtualenv and install
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Launch
.venv/bin/python scripts/server.py --port 7879
# → open http://localhost:7879
```

### Setup wizard

On first launch, a setup wizard walks you through:

1. **Admin account** — local username + password, or Google sign-in (paste an OAuth Client ID; PKCE only, no secret).
2. **System configuration** — instance name, workspace directory, and the models for each pipeline tier.
3. **Done** — config saves to `config/system.json` and Mission Control opens.

Returning later? **Settings → Setup & SSO**.

### Model configuration

Models are selected per tier — by the setup wizard on first run, or later via **Settings**, or via environment variables if you prefer config-as-code:

| Variable | Purpose |
|----------|---------|
| `TIER2_PROVIDER` | Backend type: `openai` (any OpenAI-compatible endpoint) or `gemini` (native) |
| `TIER2_MODEL` | Model id for the architect tier |
| `TIER2_BASE_URL` | Endpoint URL — point it at any OpenAI-compatible API, cloud or self-hosted |
| `TIER2_API_KEY` | Credential for the Tier 2 backend |
| `TIER2_MAX_TOKENS` | Output budget |

Tier 1 runs through [Hermes Agent](https://github.com/NousResearch/hermes-agent) and follows its model configuration; Tier 3 points at your local runtime (e.g. Ollama). Nothing about MoiraiCore assumes a particular vendor — swap any tier at any time without touching the pipeline.

---

## 🏗 The 3-Tier Pipeline

```
┌────────────────────────────────────────────────────┐
│         MoiraiCore  (Tier 1 — ScrumMaster)         │
│    backlog · task assignment · artifact evaluation │
│                                                    │
│   ┌────────────────┐    ┌────────────────┐         │
│   │     TIER 2     │    │     TIER 3     │         │
│   │   Architect    │    │  Local worker  │         │
│   │  your model    │    │  your model    │         │
│   │   blueprints   │    │   micro-fixes  │         │
│   └───────┬────────┘    └───────┬────────┘         │
│           └────────┬────────────┘                  │
│               ┌────▼──────┐                        │
│               │ ScrumGate │  merge queue → eval →  │
│               │   src/    │  merge to protected    │
│               └───────────┘  src/                  │
└────────────────────────────────────────────────────┘
```

1. **Tier 1 — ScrumMaster.** Decomposes the backlog into tasks, each carrying a spec. Assigns work, evaluates returned artifacts against spec acceptance criteria.
2. **Tier 2 — Architect.** Generates blueprints and substantial code against the spec. You pick the model — biggest context or strongest reasoning goes here.
3. **Tier 3 — Local worker.** A fast, cheap model handles micro-fixes. Point it at a local runtime and the small stuff never leaves your machine.

Then **ScrumGate**: every artifact is checked (non-empty, compiles, no dangerous patterns, spec criteria met) before it may merge into the protected `src/`. Failure sends it back with the gate's findings — the model never gets a free pass.

---

## Repository Layout

```
MoiraiCore/
├── scripts/       ← Server + orchestration (auth, goal engine, scrum, vault, workers)
├── dashboard/     ← Mission Control SPA
├── moirai/        ← Conversational intelligence engine
├── docs/          ← Design wiki
├── tests/         ← Test suite
└── config/        ← Runtime configuration (git-ignored)
```

*Customer projects, personal workspace, private vault contents, and runtime state are intentionally **not** part of this public repository.*

## Tests

```bash
cd scripts
python3 test_tier2_transport.py    # Tier 2 transport + truncation/continuation
python3 test_tier_fallback.py        # Tier 2/3 failover
python3 test_scrum_master.py         # Scrum pipeline + spec gating (needs pytest)
```

## Security

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

MoiraiCore uses JWT auth (24h access / 30d refresh), PBKDF2-SHA256 password hashing, account lockout protection, and an audit log. Secrets live in `.env`/`config/auth` and are git-ignored. Google SSO uses OAuth 2.0 + PKCE — only the Client ID is ever stored.

## License

[MIT](LICENSE)
