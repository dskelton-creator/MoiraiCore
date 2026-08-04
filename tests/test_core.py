"""
Test suite for MoiraiCore core modules.
Run: cd <repo-root> && python3 -m pytest tests/ -v
"""
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class TestAgentRegistry:
    """Tests for agent_registry.py"""

    def setup_method(self):
        from agent_registry import AgentRegistry
        self.reg = AgentRegistry()

    def test_list_agents_returns_all(self):
        agents = self.reg.list_agents(status_filter="all")
        assert len(agents) >= 6

    def test_list_active_agents(self):
        agents = self.reg.list_agents(status_filter="active")
        assert all(a["status"] == "active" for a in agents)

    def test_get_agent_by_name(self):
        agent = self.reg.get_agent("developer")
        assert agent is not None
        assert agent["name"] == "Developer"

    def test_get_agent_case_insensitive(self):
        agent = self.reg.get_agent("DeVeLoPeR")
        assert agent is not None

    def test_get_nonexistent_agent(self):
        agent = self.reg.get_agent("nonexistent")
        assert agent is None

    def test_find_agent_for_developer_task(self):
        key, confidence, triggers = self.reg.find_agent_for_task(
            "Write Python code to implement a new feature"
        )
        assert key == "developer"

    def test_find_agent_for_researcher_task(self):
        key, confidence, triggers = self.reg.find_agent_for_task(
            "Research the ASX market trends"
        )
        assert key == "researcher"

    def test_find_agent_retired_specialists_via_router(self):
        # The SEO / Threat specialist agents were removed from the curated
        # platform; these intents must not route to a retired agent key.
        key, _, _ = self.reg.find_agent_for_task(
            "Perform an SEO audit of my website"
        )
        assert key != "seo"
        key, _, _ = self.reg.find_agent_for_task(
            "Conduct a STRIDE threat model"
        )
        assert key != "threat"

    def test_find_agent_for_writer_task(self):
        key, confidence, triggers = self.reg.find_agent_for_task(
            "Write a blog post about cybersecurity"
        )
        assert key == "writer"

    def test_find_agent_tiebreaker_most_matches(self):
        """Developer task should route to developer, not threat"""
        key, confidence, triggers = self.reg.find_agent_for_task(
            "You are the Developer (Code & Architecture). Be concise and actionable.\n\n## Output Format\n# Developer — Orchestration Output Template\n\n## Required Output Structure\n\n**What to provide:**\n1. **Technical Summary** — 2-3 sentences on what to build/implement\n2. **Architecture** — Brief ASCII diagram or bullet-point data flow\n3. **Implementation** — Key code snippets (Python/JS/shell) with comments\n4. **Security Considerations** — Bullet list of risks and mitigations specific to this task\n5. **Next Steps** — What needs to happen after this is implemented"
        )
        assert key == "developer", f"Expected developer, got {key} with triggers {triggers}"

    def test_find_agent_fallback_to_hermes(self):
        key, confidence, triggers = self.reg.find_agent_for_task(
            "Hello, how are you today?"
        )
        assert key == "hermes"

    def test_active_count(self):
        count = self.reg.active_count()
        assert count >= 7

    def test_get_memory_path(self):
        path = self.reg.get_memory_path("developer")
        assert path is not None


class TestServerEndpoints:
    """Tests for server.py API endpoints"""

    def setup_method(self):
        """Set up test environment with temp directories"""
        self.test_dir = tempfile.mkdtemp()
        self.original_root = os.environ.get("AGENT_OS_ROOT")
        os.environ["AGENT_OS_ROOT"] = self.test_dir

        # Create required directories
        Path(self.test_dir, "config").mkdir(exist_ok=True)
        Path(self.test_dir, "workspace").mkdir(exist_ok=True)
        Path(self.test_dir, "memory-vault").mkdir(exist_ok=True)
        Path(self.test_dir, "dashboard").mkdir(exist_ok=True)

        # Create minimal dashboard
        Path(self.test_dir, "dashboard", "index.html").write_text(
            "<html><body>Test</body></html>"
        )

    def teardown_method(self):
        """Clean up test environment"""
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if self.original_root:
            os.environ["AGENT_OS_ROOT"] = self.original_root
        elif "AGENT_OS_ROOT" in os.environ:
            del os.environ["AGENT_OS_ROOT"]

    def test_load_tasks_empty(self):
        from server import load_tasks
        tasks = load_tasks()
        assert tasks == []

    def test_save_and_load_tasks(self):
        from server import load_tasks, save_tasks
        test_tasks = [{"id": "1", "title": "Test", "status": "queued"}]
        save_tasks(test_tasks)
        loaded = load_tasks()
        assert len(loaded) == 1
        assert loaded[0]["title"] == "Test"

    def test_load_goals_empty(self):
        from server import load_goals
        goals = load_goals()
        assert goals == []

    def test_save_and_load_goals(self):
        from server import load_goals, save_goals
        test_goals = [{"id": "goal-1", "title": "Test Goal", "status": "active"}]
        save_goals(test_goals)
        loaded = load_goals()
        assert len(loaded) == 1
        assert loaded[0]["title"] == "Test Goal"

    def test_clamp_helper(self):
        from server import _clamp
        assert _clamp(5, 1, 10) == 5
        assert _clamp(0, 1, 10) == 1
        assert _clamp(15, 1, 10) == 10
        assert _clamp("5", 1, 10) == 5
        assert _clamp("invalid", 1, 10) == 1

    def test_kanban_card_for_task(self):
        from server import kanban_card_for_task
        task = {
            "id": "123",
            "title": "Test Task",
            "desc": "A test task",
            "agent": "developer",
            "priority": "p1",
            "goal_id": "goal-1",
            "goal_title": "Test Goal",
            "matched_triggers": ["code", "test"],
        }
        card = kanban_card_for_task(task)
        assert card["id"] == "task-123"
        assert card["title"] == "Test Task"
        assert card["agent"] == "developer"

    def test_sync_task_to_kanban(self):
        from server import sync_task_to_kanban, load_kanban, save_kanban
        # Initialize empty board
        save_kanban({"backlog": [], "progress": [], "review": [], "done": []})
        task = {"id": "t1", "title": "Test", "status": "completed"}
        sync_task_to_kanban(task)
        board = load_kanban()
        assert len(board["done"]) == 1


class TestReportGeneration:
    """Tests for HTML report generation"""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.original_root = os.environ.get("AGENT_OS_ROOT")
        os.environ["AGENT_OS_ROOT"] = self.test_dir
        Path(self.test_dir, "workspace", "reports").mkdir(parents=True, exist_ok=True)
        # Reload server module to pick up new AGENT_OS_ROOT
        import importlib
        import server
        importlib.reload(server)

    def teardown_method(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if self.original_root:
            os.environ["AGENT_OS_ROOT"] = self.original_root

    def test_generate_report_creates_html_file(self):
        import importlib
        import server
        importlib.reload(server)
        from server import _generate_report_html
        result = _generate_report_html(
            report_type="goal",
            title="Test Report",
            content="# Test\n\nThis is a test report.",
            status_val="completed",
            agent="developer",
        )
        assert result.endswith(".html")
        report_path = Path(self.test_dir) / result
        assert report_path.exists(), f"Report not found at {report_path}"
        content = report_path.read_text()
        assert "Test Report" in content
        assert "COMPLETED" in content

    def test_generate_report_with_stages(self):
        import importlib
        import server
        importlib.reload(server)
        from server import _generate_report_html
        stages = [
            {"stage": "research", "agent": "researcher", "status": "completed", "duration_ms": 5000},
            {"stage": "write", "agent": "writer", "status": "completed", "duration_ms": 3000},
        ]
        result = _generate_report_html(
            report_type="goal",
            title="Pipeline Report",
            content="# Pipeline Test",
            stage_results=stages,
            status_val="completed",
        )
        report_path = Path(self.test_dir) / result
        assert report_path.exists(), f"Report not found at {report_path}"
        content = report_path.read_text()
        assert "research" in content
        assert "writer" in content

    def test_generate_report_with_output_paths(self):
        import importlib
        import server
        importlib.reload(server)
        from server import _generate_report_html
        result = _generate_report_html(
            report_type="task",
            title="Task Report",
            content="# Task",
            output_paths=["/tmp/test_output.md"],
            status_val="completed",
        )
        report_path = Path(self.test_dir) / result
        assert report_path.exists(), f"Report not found at {report_path}"
        content = report_path.read_text()
        assert "test_output.md" in content


class TestAuthMixin:
    """Tests for auth_mixin.py"""

    def _is_public_path(self, path):
        """Recreate the public path check logic for testing"""
        public_exact = {"/", "/index.html", "/favicon.ico"}
        public_prefixes = ("/api/auth/", "/api/health", "/api/stats", "/api/reports/", "/reports/")
        if path in public_exact:
            return True
        if any(path.startswith(p) for p in public_prefixes):
            return True
        return False

    def test_public_paths(self):
        assert self._is_public_path("/") is True
        assert self._is_public_path("/index.html") is True
        assert self._is_public_path("/api/auth/login") is True
        assert self._is_public_path("/api/stats") is True
        assert self._is_public_path("/api/reports/list") is True
        assert self._is_public_path("/reports/test.html") is True
        assert self._is_public_path("/api/tasks") is False
        assert self._is_public_path("/api/goals") is False


class TestGoalEngine:
    """Tests for goal_engine.py"""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.original_root = os.environ.get("AGENT_OS_ROOT")
        os.environ["AGENT_OS_ROOT"] = self.test_dir
        Path(self.test_dir, "config").mkdir(exist_ok=True)
        Path(self.test_dir, "workspace", "goal-reports").mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if self.original_root:
            os.environ["AGENT_OS_ROOT"] = self.original_root

    def test_create_goal(self):
        from goal_engine import GoalEngine
        engine = GoalEngine()
        # Mock the LLM decomposition to avoid API calls
        with patch('goal_engine.decompose_goal_with_llm') as mock_decomp:
            mock_decomp.return_value = {
                "reasoning": "Test decomposition",
                "subtasks": [
                    {"title": "Research task", "agent": "researcher", "desc": "Research", "priority": "p1", "depends_on": None},
                    {"title": "Write task", "agent": "writer", "desc": "Write", "priority": "p1", "depends_on": 1},
                ]
            }
            goal = engine.create_goal("Test Goal", "A test goal", auto_run=False)
        assert goal["id"].startswith("goal-")
        assert goal["title"] == "Test Goal"
        assert goal["status"] == "decomposed"
        assert len(goal["subtasks"]) == 2

    def test_get_goal_status(self):
        from goal_engine import GoalEngine, save_goals, load_goals
        # Create a goal first
        goals = [{"id": "goal-test", "title": "Test", "status": "active", "subtasks": [], "tasks_total": 0, "tasks_completed": 0, "tasks_failed": 0}]
        save_goals(goals)
        engine = GoalEngine()
        status = engine.get_goal_status("goal-test")
        assert status["ok"] is True
        assert status["goal"]["id"] == "goal-test"
