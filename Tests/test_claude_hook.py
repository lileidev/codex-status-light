import copy
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
HOOK = ROOT / "hooks" / "claude_status_hook.py"

BASE = {
    "hook_event_name": "SessionStart",
    "session_id": "transcript-abc",
    "cwd": "/tmp/proj",
}


class ClaudeStatusHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self._tmp.name)
        # The Claude hook resolves shared CLI + state via AGENTS_STATUS_LIGHT_*.
        (self.directory / "bin").mkdir(parents=True)
        (self.directory / "bin" / "agents-light").write_bytes(CLI.read_bytes())
        (self.directory / "bin" / "agents-light").chmod(0o755)
        self.env = os.environ.copy()
        self.env["AGENTS_STATUS_LIGHT_HOME"] = str(self.directory)
        self.env["AGENTS_STATUS_LIGHT_DIR"] = str(self.directory / "state")
        # In tests the parent is pytest, not `claude`; simulate an interactive
        # Claude parent so the parent-process gate lets the hook run.
        self.env["CLAUDE_STATUS_LIGHT_PARENT"] = "claude"

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, event):
        session_id = event.get("session_id", "transcript-abc")
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in session_id)
        target = self.directory / "state" / f"{safe}.json"
        before = target.stat().st_mtime if target.exists() else 0
        subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(event),
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        # The CLI is spawned asynchronously from the hook; wait for the write.
        for _ in range(50):
            if target.exists() and target.stat().st_mtime > before:
                return
            time.sleep(0.1)
        self.fail(f"hook did not write state file {target} for {event.get('hook_event_name')}")

    def read_state(self, session_id="transcript-abc"):
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in session_id)
        return json.loads((self.directory / "state" / f"{safe}.json").read_text())

    def test_session_start_sets_running(self):
        self.run_hook(BASE)
        self.assertEqual(self.read_state()["state"], "running")

    def test_permission_request_sets_waiting(self):
        event = dict(BASE, hook_event_name="PermissionRequest", tool_name="Bash")
        self.run_hook(event)
        self.assertEqual(self.read_state()["state"], "waiting")

    def test_post_tool_use_failure_sets_error(self):
        self.run_hook(dict(BASE, hook_event_name="SessionStart"))
        self.assertEqual(self.read_state()["state"], "running")
        self.run_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "transcript-abc",
            "tool_name": "Bash",
            "tool_response": {"is_error": True},
        })
        self.assertEqual(self.read_state()["state"], "error")

    def test_successful_tool_use_after_waiting_returns_to_running(self):
        self.run_hook(dict(BASE, hook_event_name="PermissionRequest"))
        self.assertEqual(self.read_state()["state"], "waiting")
        self.run_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "transcript-abc",
            "tool_name": "Bash",
            "tool_response": {"is_error": False},
        })
        self.assertEqual(self.read_state()["state"], "running")

    def test_stop_sets_done(self):
        self.run_hook(dict(BASE, hook_event_name="SessionStart"))
        self.run_hook({
            "hook_event_name": "Stop",
            "session_id": "transcript-abc",
            "stop_hook_active": False,
        })
        self.assertEqual(self.read_state()["state"], "done")

    def test_notification_waiting_subtype_sets_waiting(self):
        self.run_hook({
            "hook_event_name": "Notification",
            "session_id": "transcript-abc",
            "notification": {"subtype": "blocking", "message": "Waiting for approval"},
        })
        self.assertEqual(self.read_state()["state"], "waiting")

    def test_failed_tool_detection(self):
        spec = importlib.util.spec_from_file_location("claude_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.assertTrue(module.failed_tool({"tool_response": {"is_error": True}}))
        self.assertTrue(module.failed_tool({"tool_response": {"exit_code": 1}}))
        self.assertTrue(module.failed_tool({"tool_response": {"success": False}}))
        self.assertFalse(module.failed_tool({"tool_response": {"is_error": False}}))
        self.assertFalse(module.failed_tool({"tool_response": {"exit_code": 0}}))

    def test_session_id_is_stable_transcript_uuid(self):
        import unittest.mock
        spec = importlib.util.spec_from_file_location("claude_hook2", HOOK)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        # The session id is always the stable transcript UUID (never a PID):
        # using a PID with a UUID fallback was unstable across a session's hook
        # events, so one session could be split into two rows.
        with unittest.mock.patch.object(module, "_parent_process_name", return_value="claude"):
            self.assertEqual(module._session_id({"session_id": "transcript-abc"}), "transcript-abc")
        with unittest.mock.patch.object(module, "_parent_process_name", return_value="/bin/zsh"):
            self.assertEqual(module._session_id({"session_id": "transcript-abc"}), "transcript-abc")

    def test_non_claude_parent_is_skipped(self):
        # Obsidian Copilot (and similar tools) drive Claude Code programmatically,
        # so their hook parent is NOT `claude`. Those must not write a status row,
        # even though a SessionStart event arrives.
        import copy
        env = copy.copy(self.env)
        env.pop("CLAUDE_STATUS_LIGHT_PARENT", None)  # no override -> real parent (pytest)
        session_id = "0bsidian-uuid-copilot"
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in session_id)
        target = self.directory / "state" / f"{safe}.json"
        result = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(dict(BASE, hook_event_name="SessionStart",
                                  session_id=session_id)),
            capture_output=True, text=True, env=env, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        for _ in range(20):
            if target.exists():
                break
            time.sleep(0.1)
        self.assertFalse(target.exists(),
                         "Obsidian/external Claude hooks must not write a status row")


if __name__ == "__main__":
    unittest.main()