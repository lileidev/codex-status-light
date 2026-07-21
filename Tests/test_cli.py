import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "codex-status-light"
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

    def test_successful_tool_use_clears_waiting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["CODEX_STATUS_LIGHT_HOME"] = str(ROOT)
            environment["CODEX_STATUS_LIGHT_DIR"] = directory

            def run_hook(event):
                subprocess.run(
                    ["python3", str(HOOK)],
                    input=json.dumps(event),
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            run_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "approval",
                "tool_name": "Bash",
            })
            run_hook({
                "hook_event_name": "PostToolUse",
                "session_id": "approval",
                "tool_name": "Bash",
                "tool_response": {"exit_code": 0},
            })

            state = json.loads((pathlib.Path(directory) / "approval.json").read_text())
            self.assertEqual(state["state"], "running")


if __name__ == "__main__":
    unittest.main()
