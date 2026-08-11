import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agents-light"
HOOK = ROOT / "hooks" / "codex_status_hook.py"


class StatusLightTests(unittest.TestCase):
    def test_cli_writes_atomic_session_state(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [str(CLI), "waiting", "--session", "a/b", "--message", "Need input", "--state-dir", directory],
                check=True,
                capture_output=True,
                text=True,
            )
            state = json.loads((pathlib.Path(directory) / "a-b.json").read_text())
            self.assertEqual(state["state"], "waiting")
            self.assertEqual(state["message"], "Need input")

    def test_hook_failure_detection(self):
        spec = importlib.util.spec_from_file_location("status_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.assertTrue(module.failed_tool({"tool_response": {"exit_code": 1}}))
        self.assertFalse(module.failed_tool({"tool_response": {"exit_code": 0}}))

    def run_hook(self, event, environment, directory):
        target = pathlib.Path(directory) / self._safe_session(event.get("session_id", ""))
        before = target.stat().st_mtime if target.exists() else 0
        subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(event),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        # The CLI is spawned asynchronously from the hook; wait for the write.
        self._wait_for_write(target, before)

    @staticmethod
    def _safe_session(session_id):
        return "".join(c if c.isalnum() or c in "_.-" else "-" for c in session_id) + ".json"

    def _wait_for_write(self, target, before, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if target.exists() and target.stat().st_mtime > before:
                return
            time.sleep(0.05)
        self.fail(f"hook did not write state file {target}")

    def test_successful_tool_use_clears_waiting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["AGENTS_STATUS_LIGHT_HOME"] = str(ROOT)
            environment["AGENTS_STATUS_LIGHT_DIR"] = directory

            self.run_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "approval",
                "tool_name": "Bash",
            }, environment, directory)
            self.run_hook({
                "hook_event_name": "PostToolUse",
                "session_id": "approval",
                "tool_name": "Bash",
                "tool_response": {"exit_code": 0},
            }, environment, directory)

            state = json.loads((pathlib.Path(directory) / "approval.json").read_text())
            self.assertEqual(state["state"], "running")

    def test_user_prompt_submit_clears_waiting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["AGENTS_STATUS_LIGHT_HOME"] = str(ROOT)
            environment["AGENTS_STATUS_LIGHT_DIR"] = directory

            # Waiting for permission or question (yellow light).
            self.run_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "approval",
                "tool_name": "Bash",
            }, environment, directory)
            waiting_state = json.loads((pathlib.Path(directory) / "approval.json").read_text())
            self.assertEqual(waiting_state["state"], "waiting")

            # User answers/grants permission (should turn blue immediately).
            self.run_hook({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "approval",
            }, environment, directory)

            state = json.loads((pathlib.Path(directory) / "approval.json").read_text())
            self.assertEqual(state["state"], "running")
            self.assertEqual(state["is_streaming"], True)


if __name__ == "__main__":
    unittest.main()
