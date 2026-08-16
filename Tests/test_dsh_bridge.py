import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "dsh_status_light.py"

spec = importlib.util.spec_from_file_location("dsh_status_light", HOOK)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def ev(type_, time_ms=0, data=None):
    e = {"type": type_}
    if time_ms:
        e["time"] = time_ms
    e["data"] = data if data is not None else {}
    return e


class DshBridgeTests(unittest.TestCase):

    def test_parse_jsonl(self):
        self.assertEqual(module.parse_jsonl('{"type":"session"}\n{"type":"x"}\n'), [
            {"type": "session"},
            {"type": "x"},
        ])

    def test_session_cwd_reads_top_level_then_data(self):
        # DSH stores cwd at the top level of the session record; older tests
        # also placed it under data. Both must be honoured.
        self.assertEqual(
            module.session_cwd([{"type": "session", "cwd": "/top/proj"}]),
            "/top/proj",
        )
        self.assertEqual(
            module.session_cwd([{"type": "session", "data": {"cwd": "/nested"}}]),
            "/nested",
        )
        self.assertEqual(module.session_cwd([{"type": "user/message"}]), "")

    def test_session_establishes_running(self):
        state, message, streaming, last = module.status_for([
            ev("session", 1000, {"id": "s1", "cwd": "/foo"}),
        ])
        self.assertEqual(state, "running")
        self.assertEqual(message, "DSH is working")
        self.assertFalse(streaming)

    def test_prompt_and_turn_run(self):
        state, _, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("turn/start", 1100),
            ev("assistant/chunk", 1200),
        ])
        self.assertEqual(state, "running")
        self.assertTrue(streaming)

    def test_streaming_stays_on_across_tool_event_within_window(self):
        # A chunk then a tool call within the window: still actively generating,
        # so keep blinking (the tail is not a chunk but generation is ongoing).
        state, _, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("assistant/chunk", 1100),
            ev("tool/call", 1200, {"name": "bash", "callId": "c1"}),
        ])
        self.assertEqual(state, "running")
        self.assertTrue(streaming)

    def test_streaming_off_when_chunk_is_stale(self):
        # The most recent generative chunk is older than the window threshold,
        # so the light is done blinking even though it is still running.
        base = 1_000_000_0  # 10000 s
        state, _, streaming, _ = module.status_for([
            ev("user/message", base),
            ev("assistant/chunk", base),
            ev("tool/call", base + int(module.STREAMING_WINDOW_SECONDS * 1000) + 2000,
               {"name": "bash", "callId": "c2"}),
        ])
        self.assertEqual(state, "running")
        self.assertFalse(streaming)

    def test_tool_result_error_sets_error_state(self):
        state, message, streaming, _ = module.status_for([
            ev("tool/call", 1000, {"name": "bash", "callId": "c1"}),
            ev("tool/result", 1200, {"error": {"name": "ToolArgsError", "code": "INVALID_ARGS"}}),
        ])
        self.assertEqual(state, "error")
        self.assertEqual(message, "DSH tool failed")
        self.assertFalse(streaming)

    def test_tool_result_success_stays_running(self):
        state, _, _, _ = module.status_for([
            ev("tool/call", 1000, {"name": "bash", "callId": "c1"}),
            ev("tool/result", 1200, {"message": {"content": []}}),
        ])
        self.assertEqual(state, "running")

    def test_session_end_is_done(self):
        state, message, _, _ = module.status_for([ev("session/end-seed", 1000)])
        self.assertEqual(state, "done")
        self.assertEqual(message, "DSH turn completed")

    def test_turn_end_completed_is_done(self):
        # DSH emits turn/end with a reason, not session/end-seed. A completed
        # turn (before the user's next prompt) should be green.
        state, message, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("assistant/message", 1100),
            ev("turn/end", 1200, {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertEqual(state, "done")
        self.assertEqual(message, "DSH turn completed")
        self.assertFalse(streaming)

    def test_turn_end_error_is_red(self):
        state, message, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("turn/end", 1200, {"turn": 1, "reason": {"kind": "error"}}),
        ])
        self.assertEqual(state, "error")
        self.assertFalse(streaming)

    def test_turn_end_then_new_prompt_returns_to_running(self):
        state, _, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("turn/end", 1200, {"turn": 1, "reason": {"kind": "completed"}}),
            ev("user/message", 2000),
            ev("assistant/chunk", 2100),
        ])
        self.assertEqual(state, "running")
        self.assertTrue(streaming)

    def test_pending_approval_is_waiting(self):
        # An approval asked but not yet decided should turn the light yellow.
        state, message, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("approval/asked", 1100, {"id": "a1", "toolName": "bash",
                                        "reason": "escalate sandbox to danger-full-access"}),
        ])
        self.assertEqual(state, "waiting")
        self.assertIn("Approval", message)
        self.assertFalse(streaming)

    def test_decided_approval_is_not_waiting(self):
        state, _, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("approval/asked", 1100, {"id": "a1", "toolName": "bash", "reason": "x"}),
            ev("approval/decided", 1500, {"id": "a1", "outcome": "allowed-once"}),
            ev("assistant/chunk", 1600),
        ])
        self.assertEqual(state, "running")
        self.assertTrue(streaming)

    def test_pending_question_is_waiting(self):
        events = [
            ev("tool/call", 1000, {
                "name": "ask_user_question",
                "callId": "q1",
                "arguments": json.dumps({"questions": [{"question": "Pick a runtime:", "options": [{"label": "A"}]}], "id": "q1"}),
            }),
        ]
        state, message, _, _ = module.status_for(events)
        self.assertEqual(state, "waiting")
        self.assertIn("Pick a runtime", message)

    def test_resolved_question_not_waiting(self):
        events = [
            ev("tool/call", 1000, {"name": "ask_user_question", "callId": "q1", "arguments": "{}"}),
            ev("tool/result", 1200, {"message": {"content": [{"type": "tool-result", "toolCallId": "q1", "content": []}]}}),
        ]
        state, _, _, _ = module.status_for(events)
        self.assertEqual(state, "running")

    def test_last_event_wins(self):
        state, _, streaming, _ = module.status_for([
            ev("assistant/chunk", 1000),
            ev("tool/result", 1200, {"error": {"code": "E"}}),
        ])
        self.assertEqual(state, "error")
        self.assertFalse(streaming)

    def test_deep_reasoning_is_captured_as_reasoning(self):
        # Deep-dive: reasoning-chunks with no visible assistant text after them
        # should be surfaced as "reasoning" and keep the light streaming (not
        # treated as idle), so a long silent think doesn't disappear or look dead.
        state, message, streaming, _ = module.status_for([
            ev("user/message", 1000),
            ev("reasoning-chunks", 1100, {"turn": 3, "step": 1}),
            ev("reasoning-chunks", 1200, {"turn": 3, "step": 1}),
        ])
        self.assertEqual(state, "running")
        self.assertEqual(message, "DeepSeek is reasoning…(沉思中)")
        self.assertTrue(streaming)

    def test_deep_reasoning_uses_time0_timestamp(self):
        # Real DSH reasoning-chunks expose their timestamp as `time0`, not time.
        # Without honoring time0 the reasoning was invisible (last_reasoning_ts
        # stayed 0 and the session looked like plain working).
        def rc(t):
            return {"type": "reasoning-chunks", "seq0": 1, "time0": int(t * 1000),
                    "data": {"turn": 3, "step": 1}}
        state, message, streaming, _ = module.status_for([
            {"type": "user/message", "time": 1000, "data": {}},
            rc(110), rc(120),
        ])
        self.assertEqual(state, "running")
        self.assertEqual(message, "DeepSeek is reasoning…(沉思中)")
        self.assertTrue(streaming)

    def test_reasoning_then_visible_output_not_marked_reasoning(self):
        state, message, _, _ = module.status_for([
            ev("reasoning-chunks", 1000, {}),
            ev("assistant/chunk", 1100),
        ])
        self.assertEqual(state, "running")
        self.assertNotEqual(message, "DeepSeek is reasoning…(沉思中)")

    def test_log_entries_finds_nested_logs(self, tmp=None):
        with tempfile.TemporaryDirectory() as root:
            base = pathlib.Path(root)
            ws = base / "ws" / "session-uuid-1"
            ws.mkdir(parents=True)
            (ws / "session.jsonl.zstd").write_bytes(b"x")
            entries = module.log_entries(base)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0][1], "session-uuid-1")

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_decode_log_zstd(self):
        import subprocess as sp

        lines = [
            '{"type":"session","time":1000,"data":{"id":"s1","cwd":"/tmp"}}',
            '{"type":"user/message","time":1100,"data":{}}',
            '{"type":"tool/result","time":1200,"data":{"error":{"code":"E"}}}',
        ]
        with tempfile.TemporaryDirectory() as root:
            log = pathlib.Path(root) / "session.jsonl.zstd"
            sp.run(
                ["zstd", "-q", "-f", "-o", str(log)],
                input=("\n".join(lines) + "\n").encode(), check=True,
            )
            events = module.decode_log(log)
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["type"], "session")
            # The zstd-PATH robustness: candidates include absolute Homebrew paths.
            state, message, _, _ = module.status_for(events)
            self.assertEqual(state, "error")

    def test_process_log_clears_stale_session_outside_window(self):
        # A session whose last event is far beyond the active window should be
        # cleared (status row removed), not surface as an old session.
        import tempfile as _tf
        with _tf.TemporaryDirectory() as root:
            base = pathlib.Path(root)
            events = [
                {"type": "session", "time": 1000, "data": {"id": "s", "cwd": "/tmp"}},
                {"type": "user/message", "time": 1100, "data": {}},
            ]
            calls = []

            # Build a small log file (not zstd) by pointing at a fake path; we
            # monkeypatch decode/emit/clear so no real CLI or decompressor runs.
            log = base / "session.jsonl.zstd"
            log.write_bytes(b"dummy")

            original_decode = module.decode_log
            original_emit = module.emit
            original_clear = module.clear
            try:
                module.decode_log = lambda p: events
                module.clear = lambda sid: calls.append(("clear", sid))
                module.emit = lambda *a, **k: calls.append(("emit", a))

                # Force a last_ts far in the past relative to ACTIVE_WINDOW.
                module.process_log(log, "session-uuid", {}, {})
            finally:
                module.decode_log = original_decode
                module.emit = original_emit
                module.clear = original_clear

            self.assertEqual(calls, [("clear", "session-uuid")])

    def test_process_log_clears_already_seen_stale_log_on_later_scan(self):
        # The real-world bug: once a stale log's bytes are marked as seen, the
        # watcher returned early on every later scan and NEVER re-evaluated the
        # active window, so old DSH rows accumulated forever. A log that was
        # seen before (marker unchanged) must still be cleared once its last
        # activity exceeds the active window.
        import tempfile as _tf
        with _tf.TemporaryDirectory() as root:
            base = pathlib.Path(root)
            log = base / "session.jsonl.zstd"
            log.write_bytes(b"dummy")
            events = [
                {"type": "session", "time": 1000, "data": {"id": "s"}},
                # last event is far in the past -> outside the active window
            ]
            calls = []
            original_decode = module.decode_log
            original_emit = module.emit
            original_clear = module.clear
            try:
                module.decode_log = lambda p: events
                module.clear = lambda sid: calls.append(("clear-2", sid))
                module.emit = lambda *a, **k: calls.append(("emit", a))

                # Simulate a log that was already processed: its marker is in
                # `seen` (unchanged) and its old last_ts is cached. A later scan
                # must still clear it because its last activity is past the
                # active window — this is exactly the accumulation bug.
                st = log.stat()
                marker = (st.st_mtime, st.st_size)
                seen = {log: marker}
                cache = {str(log): (1.0, "running", "Codex is working", False, "/tmp")}
                module.process_log(log, "session-uuid", seen, cache)
            finally:
                module.decode_log = original_decode
                module.emit = original_emit
                module.clear = original_clear

            self.assertEqual(calls, [("clear-2", "session-uuid")],
                             "an unchanged-but-stale log must be cleared on a "
                             "later scan")


if __name__ == "__main__":
    unittest.main()