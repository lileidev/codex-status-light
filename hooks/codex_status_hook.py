#!/usr/bin/env python3
"""Translate Codex lifecycle hook JSON into status-light updates."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time


_LAUNCH_COOLDOWN_SECONDS = 10
_TOOL_THROTTLE_MS = 2000
_APP_RUNNING_CACHE_TTL_SECONDS = 5

# In-memory state mirrors opencode-status-light's JS plugin state machine.
# Keys are session IDs.
_current_state: dict[str, str] = {}
_current_streaming: dict[str, bool] = {}
_awaiting_input: set[str] = set()
_last_tool_time: dict[str, float] = {}

# Cached app-running probe to avoid pgrep on every event.
_app_running_cache: tuple[bool, float] | None = None
_app_running_lock = threading.Lock()


def install_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "CODEX_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".codex" / "status-light"),
        )
    ).expanduser()


_LOG_MAX_BYTES = 512 * 1024  # rotate hook.log when it exceeds ~512 KB


def _log(message: str) -> None:
    """Append a timestamped line to the hook log, rotating it to stay bounded."""
    try:
        log_path = install_root() / "hook.log"
        try:
            if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
                rotated = log_path.with_name("hook.log.1")
                rotated.unlink(missing_ok=True)
                log_path.rename(rotated)
        except OSError:
            pass
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except Exception:
        pass


def _app_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_APP",
            str(pathlib.Path.home() / "Applications" / "AgentsLight.app"),
        )
    ).expanduser()


def _launch_lock_path() -> pathlib.Path:
    return install_root() / ".launch-lock"


def _is_app_running() -> bool:
    """Return True if the menu-bar app is currently running.

    Result is cached for a few seconds to avoid spawning pgrep on every event.
    """
    global _app_running_cache
    with _app_running_lock:
        now = time.time()
        if _app_running_cache is not None:
            cached_value, cached_at = _app_running_cache
            if now - cached_at < _APP_RUNNING_CACHE_TTL_SECONDS:
                return cached_value

        try:
            probe = subprocess.run(
                ["/usr/bin/pgrep", "-x", "AgentsLight"],
                capture_output=True,
                timeout=2,
            )
            result = probe.returncode == 0
        except Exception as exc:
            _log(f"_is_app_running: pgrep error {exc}")
            result = False

        _app_running_cache = (result, now)
        return result


def ensure_app_running() -> None:
    """Launch the menu-bar app if it is not already running.

    Uses a short cooldown lock to avoid repeated open(1) calls when Codex fires
    several lifecycle events in quick succession. The running check is cached
    for a few seconds, and open(1) is started asynchronously so the hook does
    not block Codex.
    """
    _log("ensure_app_running: start")
    if _is_app_running():
        return

    lock = _launch_lock_path()
    try:
        if lock.exists():
            elapsed = time.time() - lock.stat().st_mtime
            _log(f"ensure_app_running: lock age={elapsed:.1f}s")
            if elapsed < _LAUNCH_COOLDOWN_SECONDS:
                _log("ensure_app_running: skipped due to cooldown")
                return
    except Exception as exc:
        _log(f"ensure_app_running: lock check error {exc}")

    app = _app_path()
    if app.exists():
        cmd = ["/usr/bin/open", "-g", str(app)]
    else:
        cmd = ["/usr/bin/open", "-g", "-a", "AgentsLight"]

    _log(f"ensure_app_running: launching with {cmd}")
    try:
        # Asynchronous launch: do not block Codex waiting for the app to start.
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"ensure_app_running: open error {exc}")
        return

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch(exist_ok=True)
        # Mark the app as running in the cache so the next events skip pgrep.
        with _app_running_lock:
            _app_running_cache = (True, time.time())
        _log("ensure_app_running: lock touched")
    except Exception as exc:
        _log(f"ensure_app_running: lock touch error {exc}")


def _parent_process_name() -> str | None:
    """Return the executable name of the parent process, e.g. ``codex``.

    Uses `ps -o comm=` (which on macOS yields the executable path). We return
    the trailing basename so callers can compare against a bare process name;
    a hook never mistakes a `~/.codex` path in a shell command for an actual
    Codex process because we only read the parent's own executable, not argv.
    """
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(os.getppid()), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value.rsplit("/", 1)[-1] if value else None
    except Exception:
        return None


def _session_id(event: dict) -> str:
    """Return a stable session ID for this Codex invocation.

    Order of preference:
      1. ``CODEX_SESSION_ID`` env var — an explicit override when Codex sets it.
      2. The event's ``session_id`` — stable for a whole session and distinct
         per session. This is required with Codex's app-server architecture:
         hooks run inside one shared ``codex app-server`` daemon, so the parent
         PID is the *same* for every session and would collapse every window
         into a single status row.
      3. The event's ``transcript_path`` stem (the rollout file), also stable
         and unique per session.
      4. The parent process PID, only when it really is a per-session ``codex``
         process and the event carried no session identity.
    """
    env_id = os.environ.get("CODEX_SESSION_ID")
    if env_id:
        return str(env_id)
    session = event.get("session_id")
    if session:
        return str(session)
    transcript = event.get("transcript_path")
    if transcript:
        stem = pathlib.Path(str(transcript)).stem
        if stem:
            return stem
    try:
        if _parent_process_name() == "codex":
            # Older Codex spawned one `codex` process per session, so its PID is
            # a stable per-window id and the app can prune the row on exit.
            return str(os.getppid())
    except Exception as exc:
        _log(f"_session_id: ppid probe error {exc}")
    return "manual"


def _question_header(event: dict) -> str:
    """Extract a short question header from user-input tool arguments."""
    args = event.get("tool_args") or event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    text = (
        args.get("message")
        or args.get("question")
        or args.get("prompt")
        or args.get("content")
        or ""
    )
    header = text.strip().splitlines()[0] if text else ""
    if len(header) > 40:
        header = header[:37] + "..."
    return header or "Question"


def _shared_root() -> pathlib.Path:
    """The shared install root both agents' hooks drive.

    The menu-bar app, CLI, and state directory live here once, shared by the
    Codex and Claude integrations. Overridable mainly for testing.
    """
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_HOME",
            str(pathlib.Path.home() / ".agents-status-light"),
        )
    ).expanduser()


def _state_dir() -> pathlib.Path:
    """The directory the shared CLI writes to (what the app's StatusStore watches)."""
    return pathlib.Path(
        os.environ.get(
            "AGENTS_STATUS_LIGHT_DIR",
            str(_shared_root() / "sessions"),
        )
    ).expanduser()


def _state_file_path(session_id: str) -> pathlib.Path:
    safe = "".join(character if character.isalnum() or character in "_.-" else "-" for character in session_id).strip("-.")
    return _state_dir() / f"{safe[:160] or 'default'}.json"


def _command() -> pathlib.Path:
    """Path to the shared status-light CLI."""
    return _shared_root() / "bin" / "agents-light"


def current_state(session_id: str) -> str | None:
    """Return the most recently written state for a session, if known."""
    cached = _current_state.get(session_id)
    if cached is not None:
        return cached
    path = _state_file_path(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("state")
    except Exception:
        return None


def emit(state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update to the shared status light.

    Runs synchronously so each write completes before the next event is
    handled. Codex fires each hook (one event) per invocation, so a synchronous
    write guarantees the on-disk status reflects the *last* event (e.g. yellow
    waiting -> blue running) in order. Forking an async subprocess per event
    previously let a stale "waiting" write finish after a newer "running" write
    and leave the light stuck on yellow. The CLI call is small (~30ms), well
    within the hook timeout.
    """
    command = _command()
    if not command.exists():
        _log(f"emit: CLI not found at {command}")
        return

    session_id = _session_id(event)
    args = [
        str(command), state,
        "--session", session_id,
        "--message", message,
        "--cwd", str(event.get("cwd") or os.getcwd()),
        "--source", "codex",
        "--quiet",
    ]
    if event.get("turn_id"):
        args.extend(["--turn", str(event["turn_id"])])
    if is_streaming:
        args.append("--streaming")

    _log(f"emit: state={state} session={session_id} streaming={is_streaming}")
    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        _log(f"emit: error {exc}")


def clear_session(session_id: str) -> None:
    """Remove a session's status row (used when Codex reports SessionEnd).

    Rows are now keyed by the session UUID rather than a per-session PID, so the
    app can no longer prune them by process liveness; clearing on SessionEnd is
    what removes a closed window immediately instead of leaving a stale row.
    """
    command = _command()
    if not command.exists():
        return
    _log(f"clear: session={session_id}")
    try:
        subprocess.run(
            [str(command), "--clear", "--session", session_id, "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        _log(f"clear: error {exc}")


def set_state(session_id: str, state: str, message: str, event: dict, is_streaming: bool = False) -> None:
    """Write a state update, mirroring opencode-status-light's setState guards."""
    if session_id in _awaiting_input and state not in ("waiting", "error"):
        _log(f"set_state: ignoring {state} because session {session_id} is awaiting input")
        return

    if (
        _current_state.get(session_id) == state
        and _current_streaming.get(session_id) == is_streaming
        and state != "running"
    ):
        _log(f"set_state: dedup skipping {state} for session {session_id}")
        return

    _current_state[session_id] = state
    _current_streaming[session_id] = is_streaming
    emit(state, message, event, is_streaming=is_streaming)


def failed_tool(event: dict) -> bool:
    response = event.get("tool_response") or event.get("tool_result") or {}
    if isinstance(response, dict):
        if response.get("is_error") is True:
            return True
        if response.get("exit_code") not in (None, 0):
            return True
    text = json.dumps(response, ensure_ascii=False).lower()
    return any(marker in text for marker in ('"is_error": true', '"exit_code": 1', "fatal:", "traceback"))


# --- Approval watcher -------------------------------------------------------
#
# Codex fires `PermissionRequest` before the approval prompt and has no hook
# for the answer itself; `PostToolUse` arrives only after a *successful* tool
# finishes (and for some calls, such as `write_stdin`, only at the next tool).
# The light would therefore stay yellow for the whole runtime of an approved
# command. This watcher closes that gap without taking over the prompt:
#
#   * when the approval carries a shell command, wait for a *new* process whose
#     command line contains a distinctive slice of that command (Codex runs it
#     as `/bin/zsh -lc <command>`), i.e. the instant execution starts;
#   * otherwise (`write_stdin`, `apply_patch`, ...) wait for the tool's output
#     to appear in the session transcript, which is the next observable event.
#
# The watcher writes `running` only while the exact waiting state it was
# started for is still on disk, so a newer approval, a denial, `Stop`, or
# `SessionEnd` always wins. A hard timeout bounds abandoned watchers.

_APPROVAL_WATCH_TIMEOUT_SECONDS = 900
_APPROVAL_WATCH_POLL_SECONDS = 0.3
_TRANSCRIPT_OUTPUT_TYPES = ("custom_tool_call_output", "function_call_output")


def _watch_dir() -> pathlib.Path:
    path = install_root() / "watch"
    path.mkdir(parents=True, exist_ok=True)
    # A watcher unlinks its job file on start; anything this old was orphaned by
    # a crash, so keep the directory from growing without bound.
    cutoff = time.time() - 3600
    for stale in path.glob("*.json"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
    return path


def _state_fingerprint(session_id: str) -> tuple[str, str, str] | None:
    """Return the identity of the current prompt on disk, if one exists."""
    try:
        data = json.loads(_state_file_path(session_id).read_text(encoding="utf-8"))
    except Exception:
        return None
    return (
        str(data.get("state") or ""),
        str(data.get("message") or ""),
        str(data.get("updated_at") or ""),
    )


def _tool_command(event: dict) -> str | None:
    """Best-effort extraction of the shell command a PermissionRequest is about."""
    for key in ("tool_input", "arguments", "tool_args", "args", "input"):
        value = event.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                continue
        if not isinstance(value, dict):
            continue
        for command_key in ("command", "cmd", "shell_command", "script"):
            candidate = value.get(command_key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return None


# Reviewers that resolve approvals without showing the user a prompt. Codex
# still fires `PermissionRequest` for them, so the hook must not paint yellow.
_AUTO_APPROVAL_REVIEWERS = {"auto_review", "auto-review", "auto", "guardian"}
_THREAD_SETTINGS_CACHE_TTL_SECONDS = 60
_THREAD_SETTINGS_SCAN_BLOCK_BYTES = 1024 * 1024
_THREAD_SETTINGS_SCAN_OVERLAP_BYTES = 64 * 1024


def _settings_cache_path(transcript: pathlib.Path) -> pathlib.Path:
    cache_dir = install_root() / "settings-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(transcript).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.json"


def _scan_thread_settings(transcript: pathlib.Path) -> dict:
    """Find the newest usable thread settings by scanning the file backwards."""
    try:
        size = transcript.stat().st_size
    except Exception:
        return {}
    position = size
    while position > 0:
        start = max(0, position - _THREAD_SETTINGS_SCAN_BLOCK_BYTES)
        try:
            with transcript.open("rb") as handle:
                handle.seek(start)
                # Overlap into the already-scanned region so a record that
                # straddles the block boundary is still parsed in full.
                chunk = handle.read(
                    position - start + _THREAD_SETTINGS_SCAN_OVERLAP_BYTES
                )
        except Exception:
            return {}
        for raw in reversed(chunk.splitlines()):
            try:
                record = json.loads(raw)
            except Exception:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "thread_settings_applied":
                continue
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and (
                settings.get("approval_policy") or settings.get("approvals_reviewer")
            ):
                return settings
        position = start
    return {}


def _latest_thread_settings(transcript_path: str | None) -> dict:
    """Return the newest thread settings recorded in a rollout transcript.

    Codex appends ``thread_settings_applied`` event messages whenever the
    approval policy or reviewer changes. A long turn can push the last such
    record megabytes away from EOF, so we cache the result for a short window
    instead of re-reading the transcript on every approval.
    """
    if not transcript_path:
        return {}
    transcript = pathlib.Path(transcript_path)
    cache_path: pathlib.Path | None = None
    try:
        cache_path = _settings_cache_path(transcript)
    except Exception:
        cache_path = None
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("path") == str(transcript)
                and isinstance(cached.get("settings"), dict)
                and time.time() - float(cached.get("read_at") or 0)
                < _THREAD_SETTINGS_CACHE_TTL_SECONDS
            ):
                return cached["settings"]
        except Exception:
            pass
    settings = _scan_thread_settings(transcript)
    if cache_path is None:
        return settings
    try:
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "path": str(transcript),
                    "read_at": time.time(),
                    "settings": settings,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, cache_path)
    except Exception:
        pass
    return settings


def _auto_reviewed(event: dict) -> bool:
    """True when Codex resolves this approval itself instead of asking the user."""
    mode = str(event.get("permission_mode") or "").strip().lower()
    if mode in {"never", "auto_review", "auto-review", "bypass"}:
        return True
    settings = _latest_thread_settings(event.get("transcript_path"))
    reviewer = str(settings.get("approvals_reviewer") or "").strip().lower()
    policy = str(settings.get("approval_policy") or "").strip().lower()
    return policy == "never" or reviewer in _AUTO_APPROVAL_REVIEWERS


def _command_needles(command: str | None) -> list[str]:
    """Return candidate slices of *command* that identify its process.

    A direct execution shows the whole command in `ps` (`/bin/zsh -lc <command>`),
    so the command prefix is the most reliable needle. When a persistent shell
    parses the command instead, the child process only sees one segment (quotes
    removed, assignments applied), so shell segments and long path-like tokens
    are added as fallbacks.
    """
    if not command:
        return []
    needles: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if len(candidate) >= 6 and candidate not in needles:
            needles.append(candidate[:48])

    first_line = command.strip().splitlines()[0] if command.strip() else ""
    if len(first_line) >= 12:
        add(first_line)
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        segment = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", segment.strip())
        line = segment.splitlines()[0] if segment else ""
        if len(line) >= 6:
            add(line)
    tokens = re.findall(r"[A-Za-z0-9_./-]{10,}", command)
    for token in sorted(tokens, key=len, reverse=True)[:2]:
        add(token)
    return needles[:5]


def _ps_rows() -> list[tuple[int, int, str]]:
    """Return (pid, ppid, command) for every process, or [] when `ps` fails."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-axww", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _matching_pids(rows: list[tuple[int, int, str]], needle: str | None) -> set[int]:
    if not needle:
        return set()
    own = os.getpid()
    return {
        pid
        for pid, _ppid, command in rows
        if pid != own and needle in command and "codex_status_hook" not in command
    }


def _transcript_output_after(path: str | None, offset: int) -> tuple[bool, int]:
    """Return (saw a tool output, new offset) for transcript bytes after offset.

    Only complete JSONL lines are consumed, so a record that is still being
    appended is picked up on the next poll instead of being skipped.
    """
    if not path:
        return False, offset
    try:
        transcript = pathlib.Path(path)
        size = transcript.stat().st_size
        if size <= offset:
            return False, offset
        with transcript.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
    except Exception:
        return False, offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return False, offset
    chunk = chunk[: end + 1]
    new_offset = offset + end + 1
    saw_output = False
    for raw in chunk.splitlines():
        try:
            record = json.loads(raw)
        except Exception:
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") in _TRANSCRIPT_OUTPUT_TYPES:
            saw_output = True
    return saw_output, new_offset


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clear_waiting(job: dict) -> None:
    """Write `running` if the prompt this watcher was started for is still up."""
    session_id = job["session_id"]
    if _state_fingerprint(session_id) != tuple(job["fingerprint"]):
        return
    emit(
        "running",
        "Codex is working",
        {
            "session_id": session_id,
            "turn_id": job.get("turn_id"),
            "cwd": job.get("cwd"),
            "transcript_path": job.get("transcript_path"),
        },
        is_streaming=False,
    )


def _spawn_approval_watcher(session_id: str, event: dict, command: str | None) -> None:
    fingerprint = _state_fingerprint(session_id)
    if fingerprint is None or fingerprint[0] != "waiting":
        return
    needles = _command_needles(command)
    job = {
        "session_id": session_id,
        "turn_id": event.get("turn_id"),
        "cwd": event.get("cwd") or os.getcwd(),
        "transcript_path": event.get("transcript_path"),
        "needles": needles,
        "fingerprint": list(fingerprint),
        "codex_pid": os.getppid(),
        "started_at": time.time(),
    }
    if not needles and not job["transcript_path"]:
        _log(f"approval_watch: nothing to observe for session={session_id}")
        return
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id).strip("-.")[:120] or "session"
    job_path = _watch_dir() / f"{safe}-{time.time_ns()}.json"
    try:
        temporary = job_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job), encoding="utf-8")
        os.replace(temporary, job_path)
        subprocess.Popen(
            [
                sys.executable or "/usr/bin/python3",
                str(pathlib.Path(__file__).resolve()),
                "--watch-approval",
                str(job_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"approval_watch: spawn error {exc}")
        job_path.unlink(missing_ok=True)
        return
    _log(
        f"approval_watch: started session={session_id} "
        f"needle_count={len(needles)} transcript={bool(job['transcript_path'])}"
    )


def _watch_approval(job_path: str) -> int:
    """Wait for an approved tool to start (or finish) and clear the yellow light."""
    path = pathlib.Path(job_path)
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"approval_watch: bad job {exc}")
        path.unlink(missing_ok=True)
        return 0
    path.unlink(missing_ok=True)

    session_id = str(job.get("session_id") or "")
    fingerprint = tuple(job.get("fingerprint") or ())
    needles = job.get("needles") or []
    if not needles and job.get("needle"):
        # Jobs written by earlier versions stored a single needle.
        needles = [job["needle"]]
    transcript = job.get("transcript_path")
    deadline = time.monotonic() + _APPROVAL_WATCH_TIMEOUT_SECONDS
    rows = _ps_rows()
    baseline: set[int] = set()
    for needle in needles:
        baseline |= _matching_pids(rows, needle)
    offset = 0
    if transcript:
        try:
            offset = pathlib.Path(transcript).stat().st_size
        except Exception:
            offset = 0

    while time.monotonic() < deadline:
        time.sleep(_APPROVAL_WATCH_POLL_SECONDS)
        if _state_fingerprint(session_id) != fingerprint:
            _log(f"approval_watch: stop session={session_id} (state changed)")
            return 0
        if not _process_alive(job.get("codex_pid")):
            _log(f"approval_watch: stop session={session_id} (codex exited)")
            return 0
        if needles:
            rows = _ps_rows()
            started: set[int] = set()
            for needle in needles:
                started |= _matching_pids(rows, needle)
            started -= baseline
            if started:
                _log(f"approval_watch: command started session={session_id} pids={sorted(started)}")
                _clear_waiting(job)
                return 0
        if transcript:
            saw_output, offset = _transcript_output_after(transcript, offset)
            if saw_output:
                _log(f"approval_watch: tool output session={session_id}")
                _clear_waiting(job)
                return 0
    _log(f"approval_watch: timeout session={session_id}")
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as exc:
        _log(f"main: failed to parse stdin: {exc}")
        return 0

    name = event.get("hook_event_name", "")
    _log(f"main: event={name}")

    # Only surface *interactive* Codex sessions. Tools that drive Codex
    # programmatically (embedded Codex, CI, other integrations) run the hook with
    # a parent process that is not `codex`, so their sessions would surface as
    # transcript-UUID rows the app cannot prune. Skip any invocation whose parent
    # isn't `codex`; this also makes _session_id() a PID that the app can clean
    # up. CODEX_STATUS_LIGHT_PARENT is a test override for controlled harnesses.
    parent = _parent_process_name()
    if os.environ.get("CODEX_STATUS_LIGHT_PARENT"):
        parent = os.environ["CODEX_STATUS_LIGHT_PARENT"]
    if parent != "codex":
        _log(f"main: skipping (parent not codex, got {parent!r}) — event={name}")
        return 0

    # Make sure the status-light app is running whenever Codex is active.
    # SessionStart is the first hook fired, so this also covers "Codex started".
    ensure_app_running()

    session_id = _session_id(event)
    tool = event.get("tool_name") or event.get("tool") or "tool"

    if name == "SessionStart":
        set_state(session_id, "running", "Codex is working", event, is_streaming=False)
    elif name == "UserPromptSubmit":
        # A new user prompt means the user has answered a waiting question or
        # granted a permission request. Clear the awaiting-input guard so the
        # light transitions from yellow back to blue immediately.
        _awaiting_input.discard(session_id)
        # Codex has no message.part.updated event; approximate streaming by marking
        # the running state as streaming until the first PostToolUse arrives.
        set_state(session_id, "running", "Codex is working", event, is_streaming=True)
    elif name == "PermissionRequest":
        auto_reviewed = _auto_reviewed(event)
        _log(
            f"PermissionRequest: tool={tool} "
            f"mode={event.get('permission_mode')} auto_reviewed={auto_reviewed}"
        )
        if auto_reviewed:
            # No prompt is shown to the user: Codex's reviewer model decides.
            # Keep the light blue and only clear a stale yellow from an earlier
            # prompt, so auto-review requests no longer flash the panel.
            _awaiting_input.discard(session_id)
            if current_state(session_id) == "waiting":
                set_state(session_id, "running", "Codex is working", event, is_streaming=False)
        else:
            _awaiting_input.add(session_id)
            set_state(session_id, "waiting", f"Approval needed: {tool}", event)
            # Codex has no hook for "the user answered"; watch the command start /
            # tool output instead so the yellow light clears as soon as the
            # approved work begins rather than at the next Codex event.
            _spawn_approval_watcher(session_id, event, _tool_command(event))
    elif name == "PreToolUse" and str(tool) in {"request_user_input", "RequestUserInput"}:
        header = _question_header(event)
        _awaiting_input.add(session_id)
        set_state(session_id, "waiting", f"Question: {header}", event)
    elif name == "PostToolUse":
        if failed_tool(event):
            set_state(session_id, "error", f"Tool failed: {tool}", event)
        elif current_state(session_id) == "waiting" or session_id in _awaiting_input:
            _awaiting_input.discard(session_id)
            set_state(session_id, "running", "Codex is working", event, is_streaming=False)
        else:
            now = time.time() * 1000
            if now - _last_tool_time.get(session_id, 0) > _TOOL_THROTTLE_MS:
                set_state(session_id, "running", "Codex is working", event, is_streaming=False)
            else:
                _log(f"PostToolUse: throttled for session {session_id}")
            _last_tool_time[session_id] = now
    elif name == "Stop":
        _current_streaming[session_id] = False
        _awaiting_input.discard(session_id)
        if current_state(session_id) != "error":
            set_state(session_id, "done", "Codex turn completed", event)
    elif name == "SessionEnd":
        # The window closed: drop its row instead of leaving a stale one, which
        # matters now that the id is a session UUID the app cannot PID-prune.
        _current_state.pop(session_id, None)
        _current_streaming.pop(session_id, None)
        _last_tool_time.pop(session_id, None)
        _awaiting_input.discard(session_id)
        clear_session(session_id)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--watch-approval":
        raise SystemExit(_watch_approval(sys.argv[2]))
    raise SystemExit(main())
