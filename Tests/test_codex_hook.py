import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "codex_status_hook.py"

spec = importlib.util.spec_from_file_location("codex_status_hook", HOOK)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
# Register under its import name so mock.patch("codex_status_hook.…") works.
sys.modules["codex_status_hook"] = module


def _fake_ps(stdout: str):
    fake = mock.Mock()
    fake.returncode = 0
    fake.stdout = stdout
    return fake


class CodexHookSessionIdTests(unittest.TestCase):

    def test_parent_process_name_normalizes_full_path_to_basename(self):
        # macOS `ps -o comm=` returns the executable *path*; the hook must derive
        # the basename so it can match a bare "codex" (a full-path string ==
        # "codex" would be False and it would wrongly fall back to unstable ids).
        with mock.patch(
            "codex_status_hook.subprocess.run",
            return_value=_fake_ps(
                "/Users/larry/.nvm/versions/node/v24.18.0/lib/node_modules/"
                "@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/"
                "aarch64-apple-darwin/bin/codex\n"
            ),
        ):
            self.assertEqual(module._parent_process_name(), "codex")

    def test_parent_process_name_against_invocation(self):
        # Simulate a shell wrapper parent: full path but not a codex binary.
        with mock.patch(
            "codex_status_hook.subprocess.run",
            return_value=_fake_ps("/usr/bin/zsh\n"),
        ):
            self.assertEqual(module._parent_process_name(), "zsh")

    def test_session_id_uses_stable_parent_pid_when_parent_is_codex(self):
        # When the parent is the Codex process, _session_id must return the
        # (stable) PID, NOT the event's session_id which varies across hooks —
        # this is what kept one Codex window producing two status rows.
        with mock.patch("codex_status_hook.os.getppid", return_value=5923), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"):
            sid = module._session_id({"session_id": "019ffdc6-052d-7c20-a086-8d6bc2cfedc7"})
        self.assertEqual(sid, "5923")

    def test_session_id_falls_back_to_event_id_when_parent_not_codex(self):
        with mock.patch("codex_status_hook._parent_process_name", return_value="zsh"):
            sid = module._session_id({"session_id": "uuid-abc"})
        self.assertEqual(sid, "uuid-abc")

    def test_session_id_prefers_env_when_set(self):
        env = {"CODEX_SESSION_ID": "env-stable"}
        with mock.patch.dict(module.os.environ, env):
            sid = module._session_id({"session_id": "event-id"})
        self.assertEqual(sid, "env-stable")

    def test_main_skips_when_parent_not_codex(self):
        # Embedded/CI Codex hooks (parent != codex) must not write any status
        # row — mirror the interactive-only rule added for Claude/Obsidian.
        import io
        import json
        event = json.dumps({"hook_event_name": "SessionStart",
                            "session_id": "uuid-abc"})
        with mock.patch("codex_status_hook.sys.stdin", io.StringIO(event)), \
             mock.patch("codex_status_hook._parent_process_name", return_value="node"), \
             mock.patch("codex_status_hook.set_state") as set_state, \
             mock.patch("codex_status_hook.ensure_app_running"):
            rc = module.main()
        self.assertEqual(rc, 0)
        set_state.assert_not_called()

    def test_main_runs_when_parent_is_codex(self):
        import io
        import json
        event = json.dumps({"hook_event_name": "SessionStart",
                            "session_id": "019ffdc6-052d-7c20-a086-8d6bc2cfedc7"})
        with mock.patch("codex_status_hook.sys.stdin", io.StringIO(event)), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"), \
             mock.patch("codex_status_hook.set_state") as set_state, \
             mock.patch("codex_status_hook.ensure_app_running"):
            module.main()
        set_state.assert_called()