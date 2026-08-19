"""Live end-to-end PoC for the Pi-harness Tier 3 executor.

Builds a temp project with a deliberately broken file, then runs
harness_worker.execute_tier3() against REAL pi + Ollama (no mocks) and checks
that the agent reads the file, edits it via its tools, and makes the test pass.

Set POC_MODEL to override the model (must be native-tool-call capable).
Exits 0 on success, 1 on failure.

Usage:
  "$HOME/.hermes/hermes-agent/venv/bin/python3" scripts/run_harness_poc.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from harness_worker import PiConfig, execute_tier3  # noqa: E402
from ollama_worker import ExecutionTask  # noqa: E402


def main() -> int:
    model = os.environ.get(
        "POC_MODEL", "hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M"
    )
    proj = tempfile.mkdtemp(prefix="poc_proj_")
    app = os.path.join(proj, "app.py")
    with open(app, "w") as fh:
        fh.write(
            "def add(a, b):\n"
            "    return a * b  # BUG: should add, not multiply\n"
            "\n\n"
            "def safe_div(a, b):\n"
            "    return a / b\n"
        )

    test = 'python3 -c "from app import add, safe_div; assert add(2,3)==5; assert safe_div(10,2)==5"'

    task = ExecutionTask(
        file_path="app.py",
        task_description=(
            "app.py contains a bug: add(a, b) should RETURN a + b but currently "
            "returns a * b. safe_div(a, b) is correct. Fix add() and leave "
            "everything else intact."
        ),
        test_command=test,
        project_space=proj,
    )

    cfg = PiConfig(model=model, max_retries=3, per_iteration_timeout=240)

    print("=== harness_worker.execute_tier3 (real pi + Ollama) ===")
    print("model:", cfg.model)
    print("project:", proj)
    res = execute_tier3(task, cfg)

    print("\n=== RESULT ===")
    print(json.dumps(res.to_dict(), indent=2))
    print("\nterminated_reason:", res.terminated_reason)
    print("success:", res.success)
    print("\n--- fixed app.py ---")
    print(open(app).read())

    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(main())