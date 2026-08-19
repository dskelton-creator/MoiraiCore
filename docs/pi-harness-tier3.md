# Pi-Harness Tier 3 Backend (MoiraiCore / Hagent)

A drop-in Tier 3 "Developer" execution engine that drives the **Pi agent harness**
([earendil-works/pi](https://github.com/earendil-works/pi)) instead of the
hand-rolled single-file rewrite loop in `ollama_worker.py`. The agent gets a
real tool loop (read / write / edit / bash), a real test-verified success gate,
and is confined to a **staging sandbox** so it can never write to `src/` directly.

## Why (the one constraint that decided the model)

Pi requires a model that exposes **native `tool_calls`** on Ollama's
OpenAI-compatible endpoint. Verified on this machine:

| model | native tool_calls | drives Pi? |
|-------|-------------------|------------|
| `qwen3:8b` (default) | YES | yes |
| `hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M` | YES | yes |
| `qwen2.5-coder:14b` | **NO** (emits the call as `<tools>…</tools>` text) | **no** |

Do **not** point Pi at `qwen2.5-coder:14b` — it returns tool calls as text, Pi
never executes anything, and the task fails with a clear "no tool executed"
message.

## How to enable

```bash
export HAGENT_TIER3_BACKEND=pi          # 'ollama' is the default (unchanged behavior)
export HAGENT_TIER3_MODEL=qwen3:8b      # optional; default qwen3:8b
export HAGENT_TIER3_PROVIDER=ollama     # optional; default ollama
export HAGENT_TIER3_TIMEOUT=240         # optional; per-agent-iteration timeout (s)
export HAGENT_TIER3_MAX_RETRIES=3       # optional
```

The toggle and model are read by `scrum_gate.generate_code_via_ollama` at call
time. Because the server loads env at startup via `os.environ.setdefault`,
set these in the process / `.env` **before** the server (or pipeline) starts and
restart it to pick them up — same rule as the other `HAGENT_*` vars.

Default (no env) keeps the original Ollama iterative loop — zero regression.

## Test-verified gate (no more silent `echo no-test`)

If a task arrives without a test command, Tier 3 now discovers a **real** one:

1. a co-located pytest file for the module (`tests/`, `test/`, or alongside) →
   `python3 -m pytest <file> -q`
2. else an import smoke test for a Python module → `python3 -c "import <module>"`
3. else `echo no-test`

This applies to **both** backends, and callers (`_worker_loop.py`,
`worker_manager.py`, `scrum_gate.execute_task`) can pass an explicit
`test_command` which wins over discovery.

## Safety: the staging sandbox

Pi has **no built-in permission system** and its default toolset includes
`bash`. So `harness_worker.execute_tier3` copies the target file into a
per-task staging dir under the project space, runs Pi with `cwd=staging`, copies
the agent's edited file back, and cleans up. The agent's tools can only see
staging. The project-space write is a single mirrored copy-back, still governed
by `scrum_gate` → merge queue → evaluate → merge. For defense-in-depth the Pi
process can additionally be dropped into the Feature-11 worker subprocess /
container (see pi's containerization docs).

## Files

| file | purpose |
|------|---------|
| `scripts/harness_worker.py` | Pi executor (drop-in for `execute_tier3`), `PiConfig.from_env()`, `effective_backend()`, `discover_test_command()` |
| `scripts/scrum_gate.py` | `generate_code_via_ollama` dispatch on `HAGENT_TIER3_BACKEND` + placeholder test resolution |
| `scripts/_worker_loop.py` | thread `test_command` from execute_task payload |
| `scripts/worker_manager.py` | thread `test_command` through the in-process fallback |
| `scripts/run_harness_poc.py` | live PoC runner (real pi + Ollama, no mocks) |
| `scripts/test_harness_worker.py` | 14 LLM-free unit tests |

## Global prerequisites (shared by both repos)

- Pi CLI installed: `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`
  (binary at `~/.local/bin/pi`).
- Ollama provider registered at `~/.pi/agent/models.json` (provider `ollama`,
  `baseUrl http://localhost:11434/v1`, models `qwen3:8b`, `qwen2.5-coder:14b`,
  Ornith).
- A tool-capable model pulled (`ollama pull qwen3:8b`).

## Live run

```bash
"$HOME/.hermes/hermes-agent/venv/bin/python3" scripts/run_harness_poc.py
```

`POC_MODEL` env overrides the model. Exits 0 on a real, test-verified fix.

> `POST /api/ollama/run` (server.py) still executes Ollama directly and bypasses
> this toggle — it is the admin "run a prompt through Ollama" endpoint, not the
> enforced Tier 3 pipeline.