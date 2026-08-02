#!/usr/bin/env python3
"""
MoiraiCore — PM Orchestrator
============================
The core pipeline execution engine with quality gates and feedback loops.
"""

import json, os, sys, time, uuid, re, subprocess
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
VAULT = AGENT_OS_ROOT / "memory-vault"
TEMPLATES_DIR = VAULT / "agents" / "orchestrations" / "templates"
RUNS_DIR = AGENT_OS_ROOT / "config" / "pm-runs"
REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "goal-reports"

sys.path.insert(0, str(SCRIPTS_DIR))

_agent_registry = None
_vault_index = None

def get_registry():
    global _agent_registry
    if _agent_registry is None:
        from agent_registry import AgentRegistry
        _agent_registry = AgentRegistry()
    return _agent_registry

def get_vault_index():
    global _vault_index
    if _vault_index is None:
        from vault_index import VaultIndex
        _vault_index = VaultIndex()
    return _vault_index

def run_hermes(args, timeout=300):
    HERMES_CLI = os.environ.get("HERMES_CLI", "")
    if not HERMES_CLI:
        for c in [
            os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes"),
            os.path.join(os.path.expanduser("~"), ".local", "bin", "hermes"),
        ]:
            if os.path.isfile(c):
                HERMES_CLI = c
                break
        else:
            HERMES_CLI = "hermes"
    cmd = [HERMES_CLI] + args
    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ})
        duration_ms = int((time.time() - start) * 1000)
        stdout = r.stdout.strip()
        session_id = None
        m = re.search(r"session_id:\s*(\S+)", stdout)
        if m:
            session_id = m.group(1)
        response_text = re.sub(r"session_id:\s*\S+\n?", "", stdout).strip()
        return {"ok": r.returncode == 0, "session_id": session_id, "response": response_text,
                "error": r.stderr.strip() if r.returncode != 0 else None, "duration_ms": duration_ms}
    except subprocess.TimeoutExpired:
        return {"ok": False, "response": None, "error": "Timed out", "duration_ms": int((time.time() - start) * 1000)}
    except FileNotFoundError:
        return {"ok": False, "response": None, "error": "Hermes CLI not found", "duration_ms": 0}
    except Exception as e:
        return {"ok": False, "response": None, "error": str(e), "duration_ms": int((time.time() - start) * 1000)}


def load_pipeline_template(template_name):
    """Load a pipeline template from the templates directory."""
    tpl_path = TEMPLATES_DIR / f"{template_name}.md"
    if tpl_path.exists():
        return tpl_path.read_text()
    return None


def quality_gate(stage_name, output_text, expected_criteria):
    """
    Evaluate stage output quality. Returns (pass: bool, feedback: str).
    Checks: completeness, actionability, structure, length.
    """
    if not output_text or len(output_text.strip()) < 100:
        return False, f"Output too short ({len(output_text or '')} chars). Need substantive content."

    score = 0
    feedback = []

    # Check length (at least 200 chars for a meaningful response)
    if len(output_text) >= 200:
        score += 1
    else:
        feedback.append("Output is too brief. Expand with more detail.")

    # Check structure (has markdown headings)
    if re.search(r'^#{1,3}\s+', output_text, re.MULTILINE):
        score += 1
    else:
        feedback.append("Add markdown headings for structure.")

    # Check for actionable content (has lists or numbered items)
    if re.search(r'^[\-\*\d]', output_text, re.MULTILINE):
        score += 1
    else:
        feedback.append("Include bullet points or numbered lists for actionable items.")

    # Check expected criteria keywords
    if expected_criteria:
        missing = [c for c in expected_criteria if c.lower() not in output_text.lower()]
        if not missing:
            score += 1
        else:
            feedback.append(f"Missing expected content: {', '.join(missing)}")

    # Pass if score >= 3 out of 4
    passed = score >= 3
    return passed, " | ".join(feedback) if feedback else "Quality check passed."


def execute_stage(stage, params, previous_outputs, run_id):
    """
    Execute a single pipeline stage via Hermes.
    Returns dict with status, output, duration_ms, error.
    """
    agent = stage["agent"]
    prompt_template = stage["prompt"]

    # Substitute params and previous outputs into prompt
    prompt = prompt_template
    for key, val in params.items():
        prompt = prompt.replace("{" + key + "}", str(val))
    for key, val in previous_outputs.items():
        prompt = prompt.replace("{" + key + "}", str(val))

    # Build Hermes args
    hermes_args = ["chat", "-q", prompt, "--quiet"]

    # Add skills if specified
    skills = stage.get("skills", [])
    for sk in skills:
        hermes_args += ["-s", sk]

    # Execute
    result = run_hermes(hermes_args, timeout=stage.get("timeout", 300))

    # Save output
    output_text = result.get("response", "")
    if output_text:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        output_file = RUNS_DIR / f"{run_id}_{stage['name']}_output.md"
        output_file.write_text(f"# {stage['name']} Output\n\nAgent: {agent}\n\n{output_text}")
        result["output_path"] = str(output_file)

    return result

from pm_orchestrator_pipelines import PIPELINE_DEFINITIONS


class PMOrchestrator:
    """
    Project Manager Orchestrator.
    Runs multi-stage pipelines with quality gates and feedback loops.
    """

    def __init__(self):
        self.run_id = None

    def list_pipelines(self):
        """List available pipeline templates."""
        result = []
        for key, pipe in PIPELINE_DEFINITIONS.items():
            result.append({
                "id": key,
                "name": pipe["name"],
                "description": pipe["description"],
                "stages": len(pipe["stages"]),
            })
        return result

    def get_pipeline(self, pipeline_id):
        """Get pipeline definition."""
        return PIPELINE_DEFINITIONS.get(pipeline_id)

    def run_pipeline(self, pipeline_id, params, max_retries=1):
        """
        Run a full pipeline with quality gates.
        Returns run record with all stage outputs.
        """
        pipeline = PIPELINE_DEFINITIONS.get(pipeline_id)
        if not pipeline:
            return {"ok": False, "error": f"Pipeline '{pipeline_id}' not found"}

        self.run_id = f"pm-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().isoformat()

        print(f"🚀 PM Pipeline: {pipeline['name']} ({self.run_id})")
        print(f"   Stages: {len(pipeline['stages'])}")

        stage_outputs = {}
        stage_results = []
        previous_output_key = None

        for i, stage in enumerate(pipeline["stages"]):
            stage_name = stage["name"]
            agent = stage["agent"]
            print(f"\n📋 Stage {i+1}/{len(pipeline['stages'])}: {stage_name} (via {agent})")

            # Build previous output references for prompt substitution
            previous_outputs = {}
            if previous_output_key:
                prev_text = stage_outputs.get(previous_output_key, "")
                previous_outputs[f"{previous_output_key}_output"] = prev_text[:3000]

            # Execute with retries
            best_result = None
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    print(f"   🔄 Retry {attempt}/{max_retries}...")

                result = execute_stage(stage, params, previous_outputs, self.run_id)
                output_text = result.get("response", "")

                if not result.get("ok"):
                    print(f"   ❌ Execution failed: {result.get('error')}")
                    best_result = result
                    continue

                # Quality gate
                criteria = stage.get("expected_criteria", [])
                passed, feedback = quality_gate(stage_name, output_text, criteria)

                if passed:
                    print(f"   ✅ Quality gate passed ({result.get('duration_ms', 0)}ms)")
                    best_result = result
                    break
                else:
                    print(f"   ⚠️  Quality gate failed: {feedback}")
                    best_result = result
                    if attempt < max_retries:
                        # Enhance prompt with feedback for retry
                        stage = dict(stage)
                        stage["prompt"] = stage["prompt"] + f"\n\n## Quality Feedback (address these issues)\n{feedback}\n"

            # Store output
            output_key = stage_name.replace("-", "_")
            stage_outputs[output_key] = best_result.get("response", "")
            previous_output_key = output_key

            stage_results.append({
                "stage": stage_name,
                "agent": agent,
                "status": "completed" if best_result.get("ok") else "failed",
                "output_path": best_result.get("output_path"),
                "duration_ms": best_result.get("duration_ms", 0),
                "error": best_result.get("error"),
            })

            if not best_result.get("ok"):
                print(f"   ⛔ Stage failed after {max_retries + 1} attempt(s). Stopping pipeline.")
                break

        # Synthesis
        synthesis_output = ""
        if "synthesis" in pipeline and stage_results[-1]["status"] == "completed":
            print(f"\n🔄 Synthesis stage...")
            synth_prompt = pipeline["synthesis"]["prompt"]
            for key, val in stage_outputs.items():
                synth_prompt = synth_prompt.replace("{" + key + "}", val[:2000])
            for key, val in params.items():
                synth_prompt = synth_prompt.replace("{" + key + "}", str(val))

            synth_result = run_hermes(["chat", "-q", synth_prompt, "--quiet"], timeout=300)
            synthesis_output = synth_result.get("response", "")

            if synthesis_output:
                REPORTS_DIR.mkdir(parents=True, exist_ok=True)
                report_file = REPORTS_DIR / f"{self.run_id}-synthesis.md"
                report_file.write_text(f"# {pipeline['name']} — Final Report\n\n{synthesis_output}")
                print(f"   📄 Synthesis saved: {report_file}")

        # Build run record
        completed = sum(1 for s in stage_results if s["status"] == "completed")
        failed = sum(1 for s in stage_results if s["status"] != "completed")

        run_record = {
            "run_id": self.run_id,
            "pipeline_id": pipeline_id,
            "pipeline_name": pipeline["name"],
            "status": "completed" if failed == 0 else "partial" if completed > 0 else "failed",
            "stages_total": len(pipeline["stages"]),
            "stages_completed": completed,
            "stages_failed": failed,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "stage_results": stage_results,
            "synthesis_path": str(REPORTS_DIR / f"{self.run_id}-synthesis.md") if synthesis_output else None,
            "params": {k: v for k, v in params.items() if k != "password"},
        }

        # Save run record
        run_file = RUNS_DIR / f"{self.run_id}.json"
        run_file.write_text(json.dumps(run_record, indent=2, default=str))

        # Log to activity database
        try:
            v = get_vault_index()
            v.log_activity(
                agent="pm",
                action=f"Pipeline: {pipeline['name']}",
                task=self.run_id,
                status=run_record["status"],
                model="pm-orchestrator",
                details=f"{completed}/{len(pipeline['stages'])} stages completed",
            )
        except Exception:
            pass

        print(f"\n📊 Pipeline {self.run_id}: {run_record['status'].upper()}")
        print(f"   {completed}/{len(pipeline['stages'])} stages completed")

        return run_record

    def get_run(self, run_id):
        """Retrieve a saved run record."""
        run_file = RUNS_DIR / f"{run_id}.json"
        if run_file.exists():
            return json.loads(run_file.read_text())
        return None

    def list_runs(self, limit=20):
        """List recent PM pipeline runs."""
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        runs = []
        for f in sorted(RUNS_DIR.glob("pm-*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(f.read_text())
                runs.append({
                    "run_id": data["run_id"],
                    "pipeline_name": data["pipeline_name"],
                    "status": data["status"],
                    "stages_completed": data["stages_completed"],
                    "stages_total": data["stages_total"],
                    "started_at": data["started_at"],
                })
            except Exception:
                pass
        return runs


def main():
    """CLI entry point for testing."""
    import argparse
    parser = argparse.ArgumentParser(description="PM Orchestrator")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List available pipelines")
    p_list.set_defaults(func=lambda args: print(json.dumps(PMOrchestrator().list_pipelines(), indent=2)))

    p_run = sub.add_parser("run", help="Run a pipeline")
    p_run.add_argument("pipeline", help="Pipeline ID")
    p_run.add_argument("--params", default="{}", help="JSON params dict")
    p_run.set_defaults(func=lambda args: print(json.dumps(
        PMOrchestrator().run_pipeline(args.pipeline, json.loads(args.params)), indent=2, default=str
    )))

    p_runs = sub.add_parser("runs", help="List recent runs")
    p_runs.set_defaults(func=lambda args: print(json.dumps(PMOrchestrator().list_runs(), indent=2)))

    p_status = sub.add_parser("status", help="Get run status")
    p_status.add_argument("run_id", help="Run ID")
    p_status.set_defaults(func=lambda args: print(json.dumps(PMOrchestrator().get_run(args.run_id), indent=2, default=str)))

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
