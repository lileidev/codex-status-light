import importlib.util
import io
import json
import pathlib
import sys
import tempfile
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

    def test_session_id_prefers_event_id_over_shared_daemon_pid(self):
        # Codex's app-server runs every hook inside one shared daemon, so the
        # parent PID is the same for all sessions. The event's session_id is the
        # stable, per-session identity and must win, or every window collapses
        # into a single status row.
        with mock.patch("codex_status_hook.os.getppid", return_value=26850), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"):
            sid = module._session_id({"session_id": "01a0c7ea-757d-7e21-91f6-a5de84b8e794"})
        self.assertEqual(sid, "01a0c7ea-757d-7e21-91f6-a5de84b8e794")

    def test_two_sessions_sharing_a_parent_pid_get_distinct_ids(self):
        with mock.patch("codex_status_hook.os.getppid", return_value=26850), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"):
            first = module._session_id({"session_id": "01a0c7ea-757d-7e21-91f6-a5de84b8e794"})
            second = module._session_id({"session_id": "01a0d2ab-9b39-7063-8e0d-6487251cce3c"})
        self.assertNotEqual(first, second)

    def test_session_id_falls_back_to_transcript_stem(self):
        with mock.patch("codex_status_hook._parent_process_name", return_value="codex"):
            sid = module._session_id({
                "transcript_path": "/Users/x/.codex/sessions/2026/09/22/"
                                   "rollout-2026-09-22T15-00-26-01a0c7ea-757d-7e21-91f6-a5de84b8e794.jsonl"
            })
        self.assertEqual(sid, "rollout-2026-09-22T15-00-26-01a0c7ea-757d-7e21-91f6-a5de84b8e794")

    def test_session_id_uses_parent_pid_when_event_has_no_identity(self):
        with mock.patch("codex_status_hook.os.getppid", return_value=5923), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"):
            sid = module._session_id({})
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

    def test_main_session_end_clears_the_row(self):
        # A closed window must drop its row; with UUID ids the app can no longer
        # prune it by process liveness, so SessionEnd is the cleanup path.
        import io
        import json
        event = json.dumps({"hook_event_name": "SessionEnd",
                            "session_id": "01a0c7ea-757d-7e21-91f6-a5de84b8e794"})
        with mock.patch("codex_status_hook.sys.stdin", io.StringIO(event)), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"), \
             mock.patch("codex_status_hook.clear_session") as clear_session, \
             mock.patch("codex_status_hook.set_state") as set_state, \
             mock.patch("codex_status_hook.ensure_app_running"):
            rc = module.main()
        self.assertEqual(rc, 0)
        clear_session.assert_called_once_with("01a0c7ea-757d-7e21-91f6-a5de84b8e794")
        set_state.assert_not_called()


class CodexApprovalWatcherTests(unittest.TestCase):
    """The watcher bridges the missing "user answered the approval" hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    def _write_status(self, message="Approval needed: Bash"):
        payload = {
            "state": "waiting",
            "message": message,
            "updated_at": "2026-11-09T00:00:00Z",
            "source": "codex",
            "is_streaming": False,
        }
        (self.tmp / "sess-1.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _write_job(self, fingerprint, needles=None, transcript_path=None):
        job = {
            "session_id": "sess-1",
            "turn_id": "turn-1",
            "cwd": "/tmp",
            "transcript_path": transcript_path,
            "needles": needles or [],
            "fingerprint": list(fingerprint),
            "codex_pid": module.os.getpid(),
        }
        path = self.tmp / "job.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        return path

    def _fingerprint(self, status):
        return [status["state"], status["message"], status["updated_at"]]

    def test_command_needles_include_prefix_segments_and_tokens(self):
        command = (
            "PYTHONPATH=python/perfopt/src /very/long/venv/bin/python run.py "
            "&& cat /tmp/output.log"
        )
        needles = module._command_needles(command)
        self.assertIn(command[:48], needles)
        self.assertIn("/very/long/venv/bin/python run.py", needles)
        self.assertIn("cat /tmp/output.log", needles)

    def test_command_needles_split_shell_operators(self):
        needles = module._command_needles("sleep 4; : marker")
        self.assertIn("sleep 4", needles)
        self.assertIn(": marker", needles)

    def test_command_needles_ignore_empty_commands(self):
        self.assertEqual(module._command_needles(None), [])
        self.assertEqual(module._command_needles("   "), [])

    def test_tool_command_reads_json_string_and_command_key(self):
        event = {"tool_input": json.dumps({"command": "python3 - <<'PY'\nprint(1)\nPY"})}
        self.assertEqual(module._tool_command(event), "python3 - <<'PY'\nprint(1)\nPY")
        self.assertIsNone(module._tool_command({"tool_input": {"session_id": 3}}))

    def test_matching_pids_skips_hook_processes(self):
        rows = [
            (101, 1, "/bin/zsh -lc python3 - <<'PY'"),
            (102, 101, "/usr/bin/python3 /x/hooks/codex_status_hook.py --watch-approval"),
            (103, 1, "/usr/bin/python3 /x/hooks/codex_status_hook.py"),
        ]
        self.assertEqual(module._matching_pids(rows, "python3"), {101})

    def test_transcript_output_after_reads_only_complete_lines(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text("", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"payload": {"type": "custom_tool_call_output"}}')
        saw, new_offset = module._transcript_output_after(str(path), offset)
        self.assertFalse(saw)
        self.assertEqual(new_offset, offset)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        saw, new_offset = module._transcript_output_after(str(path), offset)
        self.assertTrue(saw)
        self.assertGreater(new_offset, offset)

    def test_transcript_output_after_ignores_non_output_records(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text('{"payload": {"type": "custom_tool_call"}}\n', encoding="utf-8")
        saw, _ = module._transcript_output_after(str(path), 0)
        self.assertFalse(saw)

    def test_spawn_approval_watcher_writes_job_and_starts_process(self):
        with mock.patch.object(module, "_state_dir", return_value=self.tmp):
            status = self._write_status()
            with mock.patch.object(module, "_watch_dir", return_value=self.tmp), \
                 mock.patch.object(module.subprocess, "Popen") as popen:
                module._spawn_approval_watcher(
                    "sess-1",
                    {"turn_id": "turn-1", "cwd": "/tmp",
                     "transcript_path": "/tmp/rollout.jsonl"},
                    "python3 /Users/x/run_profiling_check.py",
                )
        popen.assert_called_once()
        self.assertIn("--watch-approval", popen.call_args.args[0])
        jobs = list(self.tmp.glob("sess-1-*.json"))
        self.assertEqual(len(jobs), 1)
        job = json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertIn("python3 /Users/x/run_profiling_check.py", job["needles"])
        self.assertEqual(job["fingerprint"], self._fingerprint(status))

    def test_watch_approval_clears_when_command_starts(self):
        with mock.patch.object(module, "_state_dir", return_value=self.tmp):
            status = self._write_status()
            job_path = self._write_job(
                self._fingerprint(status), needles=["run_profiling_check.py"]
            )
            baseline = [(10, 1, "/bin/zsh -lc something-else")]
            started = baseline + [
                (11, 1, "/bin/zsh -lc python3 run_profiling_check.py")
            ]
            with mock.patch.object(
                module, "_ps_rows", side_effect=[baseline, started, started]
            ), mock.patch.object(module.time, "sleep"), \
                 mock.patch.object(module, "emit") as emit:
                module._watch_approval(str(job_path))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "running")
        self.assertFalse(job_path.exists())

    def test_watch_approval_stops_when_the_prompt_is_replaced(self):
        with mock.patch.object(module, "_state_dir", return_value=self.tmp):
            status = self._write_status()
            job_path = self._write_job(
                self._fingerprint(status), needles=["run_profiling_check.py"]
            )
            changed = ("running", "Codex is working", "later")
            with mock.patch.object(
                module, "_ps_rows", return_value=[(10, 1, "other")]
            ), mock.patch.object(
                module, "_state_fingerprint",
                side_effect=[self._fingerprint(status), changed],
            ), mock.patch.object(module.time, "sleep"), \
                 mock.patch.object(module, "emit") as emit:
                module._watch_approval(str(job_path))
        emit.assert_not_called()

    def test_watch_approval_clears_on_tool_output_without_a_process(self):
        with mock.patch.object(module, "_state_dir", return_value=self.tmp):
            status = self._write_status(message="Approval needed: write_stdin")
            job_path = self._write_job(
                self._fingerprint(status), transcript_path="/tmp/rollout.jsonl"
            )
            with mock.patch.object(
                module, "_transcript_output_after", side_effect=[(True, 10)]
            ), mock.patch.object(module.time, "sleep"), \
                 mock.patch.object(module, "emit") as emit:
                module._watch_approval(str(job_path))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "running")

    def test_watch_approval_also_tails_the_transcript_with_needles(self):
        with mock.patch.object(module, "_state_dir", return_value=self.tmp):
            status = self._write_status()
            job_path = self._write_job(
                self._fingerprint(status),
                needles=["run_profiling_check.py"],
                transcript_path="/tmp/rollout.jsonl",
            )
            with mock.patch.object(
                module, "_ps_rows", return_value=[(10, 1, "other")]
            ), mock.patch.object(
                module, "_transcript_output_after", side_effect=[(True, 10)]
            ), mock.patch.object(module.time, "sleep"), \
                 mock.patch.object(module, "emit") as emit:
                module._watch_approval(str(job_path))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "running")


class CodexAutoReviewTests(unittest.TestCase):
    """Auto-review approvals never reach the user, so they must not turn yellow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        root_patch = mock.patch.object(module, "install_root", return_value=self.tmp)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def _transcript(self, settings):
        path = self.tmp / "rollout.jsonl"
        path.write_text(
            json.dumps({"payload": {"type": "thread_settings_applied",
                                    "thread_settings": settings}}) + "\n",
            encoding="utf-8",
        )
        return str(path)

    def _run(self, event, current="running"):
        with mock.patch("codex_status_hook.sys.stdin", io.StringIO(json.dumps(event))), \
             mock.patch("codex_status_hook._parent_process_name", return_value="codex"), \
             mock.patch("codex_status_hook.ensure_app_running"), \
             mock.patch("codex_status_hook.current_state", return_value=current), \
             mock.patch("codex_status_hook.set_state") as set_state, \
             mock.patch("codex_status_hook._spawn_approval_watcher") as spawn:
            rc = module.main()
        return rc, set_state, spawn

    def test_latest_thread_settings_returns_the_newest_record(self):
        path = self._transcript({"approval_policy": "on-request",
                                 "approvals_reviewer": "user"})
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "payload": {"type": "thread_settings_applied",
                            "thread_settings": {"approval_policy": "never",
                                                "approvals_reviewer": "auto_review"}}
            }) + "\n")
        settings = module._latest_thread_settings(path)
        self.assertEqual(settings["approvals_reviewer"], "auto_review")

    def test_latest_thread_settings_handles_missing_file(self):
        self.assertEqual(module._latest_thread_settings(None), {})
        self.assertEqual(module._latest_thread_settings(str(self.tmp / "missing")), {})

    def test_auto_reviewed_from_permission_mode(self):
        self.assertTrue(module._auto_reviewed({"permission_mode": "never"}))
        self.assertTrue(module._auto_reviewed({"permission_mode": "auto_review"}))
        self.assertFalse(module._auto_reviewed({"permission_mode": "on-request"}))

    def test_auto_reviewed_from_transcript_reviewer(self):
        transcript = self._transcript({"approval_policy": "on-request",
                                       "approvals_reviewer": "auto_review"})
        self.assertTrue(module._auto_reviewed({"transcript_path": transcript}))

    def test_auto_reviewed_false_for_user_reviewer(self):
        transcript = self._transcript({"approval_policy": "on-request",
                                       "approvals_reviewer": "user"})
        self.assertFalse(module._auto_reviewed({"transcript_path": transcript}))

    def test_main_auto_review_does_not_wait_or_spawn(self):
        event = {"hook_event_name": "PermissionRequest", "session_id": "sess-auto",
                 "tool_name": "Bash", "permission_mode": "never",
                 "tool_input": {"command": "git status"}}
        rc, set_state, spawn = self._run(event)
        self.assertEqual(rc, 0)
        set_state.assert_not_called()
        spawn.assert_not_called()
        self.assertNotIn("sess-auto", module._awaiting_input)

    def test_main_auto_review_clears_a_stale_waiting_state(self):
        event = {"hook_event_name": "PermissionRequest", "session_id": "sess-auto2",
                 "tool_name": "Bash", "permission_mode": "never",
                 "tool_input": {"command": "git status"}}
        rc, set_state, spawn = self._run(event, current="waiting")
        self.assertEqual(rc, 0)
        set_state.assert_called_once()
        self.assertEqual(set_state.call_args.args[1], "running")
        spawn.assert_not_called()

    def test_main_user_review_still_waits_and_spawns(self):
        event = {"hook_event_name": "PermissionRequest", "session_id": "sess-user",
                 "tool_name": "Bash", "permission_mode": "on-request",
                 "tool_input": {"command": "git status"}, "cwd": "/tmp"}
        rc, set_state, spawn = self._run(event)
        self.assertEqual(rc, 0)
        set_state.assert_called_once()
        self.assertEqual(set_state.call_args.args[1], "waiting")
        spawn.assert_called_once()
