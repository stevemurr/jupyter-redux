"""TCP execution server that runs inside each notebook container.

Listens on a TCP port, maintains a persistent Python namespace,
executes submitted code, and streams stdout/stderr back to the client.
"""

import base64
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback

HOST = "0.0.0.0"
PORT = 9999
BUFFER_SIZE = 65536
MAX_DISPLAY_BYTES = 10 * 1024 * 1024  # 10MB

namespaces: dict[str, dict] = {}
current_thread: threading.Thread | None = None
# Reference to the current connection for display functions
_current_conn: socket.socket | None = None
# Serializes writes to the client socket — the exec thread (stdout/stderr,
# displays) and the interrupt cascade daemon can both call send_message.
_send_lock = threading.Lock()
# Thread ids that existed before user code started running. Used by the
# interrupt cascade to identify "child" threads spawned by user code so
# we can post KeyboardInterrupt to them as well.
_exec_baseline_thread_ids: set[int] = set()
# Per-execution auto-detector that watches stdout for training metrics
# and renders a live chart when it locks onto a pattern. Set by
# handle_message at the start of each execute, cleared on completion.
# See PatternLocker for the detection rules.
_current_locker: "PatternLocker | None" = None

# ---------------------------------------------------------------------------
#  Directory creation tracking
# ---------------------------------------------------------------------------
_created_dirs: set[str] = set()
_created_dirs_lock = threading.Lock()

# Only track directories created under these roots
_TRACKED_ROOTS = ("/env/files", "/env/repos")

# Directories created by libraries/runtime that we don't care about
_DIR_BLOCKLIST = {
    "__pycache__", ".git", ".hg", ".svn",
    "node_modules", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox",
}

_original_os_mkdir = os.mkdir
_original_os_makedirs = os.makedirs
_original_path_mkdir = __import__("pathlib").Path.mkdir


def _should_track(abspath):
    basename = os.path.basename(abspath)
    if basename in _DIR_BLOCKLIST:
        return False
    return any(abspath.startswith(r + "/") or abspath == r for r in _TRACKED_ROOTS)


def _tracking_os_mkdir(path, *args, **kwargs):
    _original_os_mkdir(path, *args, **kwargs)
    abspath = os.path.abspath(path)
    if _should_track(abspath):
        with _created_dirs_lock:
            _created_dirs.add(abspath)


def _tracking_os_makedirs(name, *args, **kwargs):
    _original_os_makedirs(name, *args, **kwargs)
    abspath = os.path.abspath(name)
    if _should_track(abspath):
        with _created_dirs_lock:
            _created_dirs.add(abspath)


def _tracking_path_mkdir(self, *args, **kwargs):
    _original_path_mkdir(self, *args, **kwargs)
    abspath = str(self.resolve())
    if _should_track(abspath):
        with _created_dirs_lock:
            _created_dirs.add(abspath)


os.mkdir = _tracking_os_mkdir
os.makedirs = _tracking_os_makedirs
__import__("pathlib").Path.mkdir = _tracking_path_mkdir


def _drain_created_dirs() -> list[str]:
    with _created_dirs_lock:
        dirs = sorted(_created_dirs)
        _created_dirs.clear()
    return dirs


def _get_namespace(notebook_id: str = "") -> dict:
    """Return the namespace for a notebook, creating it if needed."""
    key = notebook_id or "__default__"
    if key not in namespaces:
        namespaces[key] = {}
    return namespaces[key]


def send_message(conn: socket.socket, msg: dict) -> None:
    # Feed stdout lines through the auto-monitor before they go out,
    # so any synthesized chart messages are written just ahead of the
    # text that triggered them. Wrapped in try/except so a detector
    # bug can never break the stdout path.
    if (
        _current_locker is not None
        and msg.get("type") == "output"
        and msg.get("stream") == "stdout"
    ):
        try:
            _current_locker.feed(msg.get("text", ""))
        except Exception:
            pass
    data = (json.dumps(msg) + "\n").encode("utf-8")
    with _send_lock:
        conn.sendall(data)


AUDIO_MIMES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
}


def display_audio(path: str, mime: str | None = None) -> None:
    """Display an audio file in the notebook output.

    Args:
        path: Path to the audio file inside the container.
        mime: MIME type override (auto-detected from extension if omitted).
    """
    conn = _current_conn
    if conn is None:
        raise RuntimeError("display_audio can only be called during cell execution")

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Audio file not found: {path}")

    size = os.path.getsize(path)
    if size > MAX_DISPLAY_BYTES:
        raise ValueError(
            f"Audio file too large ({size // 1024 // 1024}MB). "
            f"Max is {MAX_DISPLAY_BYTES // 1024 // 1024}MB."
        )

    if mime is None:
        ext = os.path.splitext(path)[1].lower()
        mime = AUDIO_MIMES.get(ext) or mimetypes.guess_type(path)[0]
    if mime is None:
        mime = "audio/wav"

    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")

    send_message(conn, {
        "type": "display",
        "display_type": "audio",
        "mime": mime,
        "data": data,
        "filename": os.path.basename(path),
    })


class StreamingWriter:
    """File-like object that sends each write() over the socket immediately.

    Reports isatty() as True so that libraries like tqdm use their
    \\r-overwrite progress-bar mode instead of emitting one line per
    update. The frontend stream processor interprets \\r and strips
    ANSI escape sequences. This does mean other libraries will emit
    ANSI color codes (we drop them), losing terminal color — worth it
    for readable progress bars.
    """

    # tqdm and other libraries introspect these on a TTY-like stream.
    encoding = "utf-8"

    def __init__(self, conn: socket.socket, stream: str) -> None:
        self._conn = conn
        self._stream = stream

    def write(self, text: str) -> int:
        if text:
            send_message(self._conn, {
                "type": "output",
                "stream": self._stream,
                "text": text,
            })
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("StreamingWriter has no fileno")


# Project-scoped install model. Cells like `pip install numpy` are
# intercepted and rewritten to uv project commands (`uv add numpy`)
# so everything ends up recorded in pyproject.toml and the lockfile.
# Any `uv <anything>` cell passes through verbatim — this covers
# `uv add`, `uv remove`, `uv sync`, `uv lock`, `uv run`, `uv tree`,
# `uv pip install` (untracked escape hatch), and anything uv may add
# in the future — without requiring a code update for each one.
#
# pyproject.toml lives at PROJECT_DIR. All uv commands run with
# cwd=PROJECT_DIR so uv finds the project without upward-search
# ambiguity.
PROJECT_DIR = "/env/files"


# Pip subcommands we remap to uv equivalents. `pip install` becomes
# `uv add` (tracked), `pip uninstall` becomes `uv remove` (tracked),
# `pip list`/`pip show` become `uv pip list`/`uv pip show` (untracked
# reads). Anything else under `pip ...` isn't claimed by this detector.
_PIP_SUBCOMMANDS = ("install", "uninstall", "list", "show")


def is_pip_command(code: str) -> bool:
    """Match any `uv ...` cell plus the specific `pip ...` subcommands
    we remap. Permissive on uv so new uv subcommands (uv run, uv tree,
    uv tool, etc.) work without touching this file."""
    stripped = code.strip()
    if stripped == "uv" or stripped.startswith("uv "):
        return True
    for sub in _PIP_SUBCOMMANDS:
        if stripped.startswith(f"pip {sub}"):
            return True
    return False


def _tokenize_cell(code: str) -> list[str]:
    return code.strip().split()


_PIP_TO_UV = {
    "install": ["uv", "add"],
    "uninstall": ["uv", "remove"],
    "list": ["uv", "pip", "list"],
    "show": ["uv", "pip", "show"],
}


def _rewrite_pip(tokens: list[str]) -> list[str] | None:
    if len(tokens) < 2:
        return None
    prefix = _PIP_TO_UV.get(tokens[1])
    if prefix is None:
        return None
    return [*prefix, *tokens[2:]]


def _build_uv_command(code: str) -> list[str] | None:
    """Rewrite a pip/uv cell into the uv argv to actually execute.

    `pip install/uninstall/list/show` are remapped to their uv
    equivalents. `uv ...` cells pass through verbatim so any uv
    subcommand — `run`, `tree`, `tool`, etc. — works without
    per-subcommand handling.
    """
    tokens = _tokenize_cell(code)
    if not tokens:
        return None
    if tokens[0] == "uv":
        # Permissive passthrough — uv itself will surface a clear
        # error if the subcommand is invalid.
        return list(tokens)
    if tokens[0] == "pip":
        return _rewrite_pip(tokens)
    return None


_REQ_LINE_RE = re.compile(
    r"^[a-zA-Z0-9][\w.*-]*"  # package name
    r"(\[[\w,.-]+\])?"       # optional extras [extra1,extra2]
    r"([<>=!~]+[\w.*]+)?"    # optional version spec >=1.0
    r"(,\s*[<>=!~]+[\w.*]+)*$"  # optional additional specifiers
)


def is_requirements_block(code: str) -> bool:
    """Check if the code looks like requirements.txt content."""
    lines = code.strip().splitlines()
    if not lines:
        return False
    pkg_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _REQ_LINE_RE.match(stripped):
            return False
        pkg_count += 1
    return pkg_count >= 2


def requirements_to_uv_add(code: str) -> list[str]:
    """Convert a requirements.txt-style cell to a single `uv add` argv."""
    pkgs = []
    for line in code.strip().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            pkgs.append(stripped)
    return ["uv", "add", *pkgs]


def is_shell_command(code: str) -> bool:
    return code.strip().startswith("!")


def is_apt_command(code: str) -> bool:
    stripped = code.strip()
    return stripped.startswith(
        ("apt install", "apt-get install", "apt remove", "apt-get remove"),
    )


def _parse_apt_cell(code: str) -> tuple[str, list[str]]:
    """Split an apt cell into (subcommand, list-of-args).

    Accepts both ``apt`` and ``apt-get`` prefixes. Keeps any user-
    supplied flags (e.g. ``--reinstall``) since they get passed
    through to apt-get verbatim.
    """
    stripped = code.strip()
    if stripped.startswith("apt-get "):
        rest = stripped[len("apt-get "):]
    else:
        rest = stripped[len("apt "):]
    tokens = rest.split()
    if not tokens:
        return "install", []
    return tokens[0], tokens[1:]


def execute_apt(conn: socket.socket, code: str) -> bool:
    """Run `apt install` / `apt remove` as root via sudo.

    Requires the Dockerfile's sudoers grant for ``jredux`` to run
    ``/usr/bin/apt-get`` without a password. Runs ``apt-get update``
    first because our image builds strip ``/var/lib/apt/lists`` to
    save space, so a fresh container can't resolve package names.
    Returns True if any step errored (so handle_message sends the
    right terminal state).
    """
    subcommand, extra_args = _parse_apt_cell(code)
    env = {
        **os.environ,
        "DEBIAN_FRONTEND": "noninteractive",
        "PYTHONUNBUFFERED": "1",
    }

    def _run(cmd: list[str]) -> int:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        for line in iter(process.stdout.readline, ""):
            send_message(conn, {
                "type": "output", "stream": "stdout", "text": line,
            })
        process.wait()
        return process.returncode

    try:
        # Refresh the package list (stripped during image build).
        if subcommand in ("install",):
            rc = _run(["sudo", "-n", "apt-get", "update"])
            if rc != 0:
                send_message(conn, {
                    "type": "error",
                    "ename": "AptError",
                    "evalue": f"apt-get update failed with exit {rc}",
                    "traceback": [],
                })
                return True

        rc = _run([
            "sudo", "-n", "apt-get", subcommand,
            "-y", "--no-install-recommends", *extra_args,
        ])
        if rc != 0:
            send_message(conn, {
                "type": "error",
                "ename": "AptError",
                "evalue": f"apt-get {subcommand} failed with exit {rc}",
                "traceback": [],
            })
            return True

        # Quiet reminder that container-level installs are ephemeral.
        send_message(conn, {
            "type": "output",
            "stream": "stderr",
            "text": (
                "\n[apt] changes apply to this container only — "
                "they're lost if the container is restarted or "
                "recreated. Add persistent deps to the executor "
                "Dockerfile when you're sure you want them.\n"
            ),
        })
        return False
    except Exception as e:
        send_message(conn, {
            "type": "error",
            "ename": type(e).__name__,
            "evalue": str(e),
            "traceback": traceback.format_exception(e),
        })
        return True


def execute_uv(conn: socket.socket, args: list[str]) -> bool:
    """Run a uv command streaming stdout/stderr back to the client.

    cwd is PROJECT_DIR so uv finds pyproject.toml without any upward-
    search ambiguity. Returns True on failure so the caller sets the
    right terminal cell state.
    """
    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=PROJECT_DIR,
        )
        for line in iter(process.stdout.readline, ""):
            send_message(conn, {
                "type": "output", "stream": "stdout", "text": line,
            })
        process.wait()
        send_message(conn, {
            "type": "result",
            "data": f"Exit code: {process.returncode}",
        })
        return process.returncode != 0
    except Exception as e:
        send_message(conn, {
            "type": "error",
            "ename": type(e).__name__,
            "evalue": str(e),
            "traceback": traceback.format_exception(e),
        })
        return True


def execute_shell(conn: socket.socket, command: str) -> None:
    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        for line in iter(process.stdout.readline, ""):
            send_message(conn, {"type": "output", "stream": "stdout", "text": line})
        process.wait()
        send_message(conn, {
            "type": "result",
            "data": f"Exit code: {process.returncode}",
        })
    except Exception as e:
        send_message(conn, {
            "type": "error",
            "ename": type(e).__name__,
            "evalue": str(e),
            "traceback": traceback.format_exception(e),
        })


FILES_DIR = "/env/files"
REPOS_DIR = "/env/repos"


def _refresh_pth_paths() -> None:
    """Re-process .pth files so editable installs done after the
    executor started are importable.

    Modern editable installs (PEP 660, setuptools >= 64) use .pth
    files whose content is a Python ``import`` statement that
    registers a custom finder on ``sys.meta_path`` — not a plain
    directory path. ``site.addsitedir`` correctly handles both
    styles: it execs import lines and appends directory lines to
    sys.path. The previous manual os.path.isdir check silently
    skipped the import-style files.
    """
    import importlib
    import site

    for sp_dir in site.getsitepackages():
        try:
            site.addsitedir(sp_dir)
        except Exception:
            pass
    importlib.invalidate_caches()


def _invalidate_user_modules() -> None:
    """Remove cached modules from /env/files and /env/repos so
    re-imports pick up the latest source from disk."""
    _refresh_pth_paths()
    to_remove = []
    for name, mod in sys.modules.items():
        path = getattr(mod, "__file__", None)
        if path and (path.startswith(FILES_DIR) or path.startswith(REPOS_DIR)):
            to_remove.append(name)
    for name in to_remove:
        del sys.modules[name]


# ---------------------------------------------------------------------------
#  Monitor — real-time training metrics
# ---------------------------------------------------------------------------

class Monitor:
    """Live training monitor that streams metrics to the notebook UI.

    Usage (explicit):
        mon = Monitor(title="Training", total_steps=100)
        for epoch in range(100):
            loss = train_step(...)
            mon.log(epoch=epoch, loss=loss)
        mon.done()

    Usage (wrap):
        mon = Monitor()
        with mon.wrap():
            train_vae(loader)  # stdout parsed for numeric patterns
    """

    _counter = 0
    _THROTTLE_INTERVAL = 0.05  # 50ms = max 20 updates/sec

    def __init__(self, title="Training Monitor", total_steps=None):
        Monitor._counter += 1
        self._display_id = f"mon_{Monitor._counter}_{id(self):x}"
        self._title = title
        self._total_steps = total_steps
        self._step = 0
        self._keys: list[str] = []
        self._last_send = 0.0
        self._pending: dict | None = None
        self._pending_step: int | None = None
        self._send_init()

    def _send(self, msg: dict) -> None:
        conn = _current_conn
        if conn is None:
            return
        send_message(conn, msg)

    def _send_init(self) -> None:
        self._send({
            "type": "display",
            "display_type": "monitor",
            "display_id": self._display_id,
            "action": "init",
            "config": {
                "title": self._title,
                "total_steps": self._total_steps,
            },
        })

    def log(self, step=None, **metrics):
        """Log metrics for one step. Auto-increments step if omitted.

        If epoch/step/iter is passed as a metric and step= is not set,
        it is extracted and used as the step value.
        """
        if step is None:
            for key in ("epoch", "step", "iter", "iteration"):
                if key in metrics:
                    step = int(metrics.pop(key))
                    break
        if step is None:
            step = self._step
            self._step += 1
        else:
            self._step = step + 1

        for k in metrics:
            if k not in self._keys:
                self._keys.append(k)

        now = time.time()
        if now - self._last_send < self._THROTTLE_INTERVAL:
            self._pending = metrics
            self._pending_step = step
            return
        self._last_send = now
        self._send_update(step, metrics)

    def _send_update(self, step, metrics):
        self._send({
            "type": "display",
            "display_type": "monitor",
            "display_id": self._display_id,
            "action": "update",
            "step": step,
            "metrics": metrics,
            "ts": time.time(),
        })

    def _flush_pending(self):
        if self._pending is not None:
            self._send_update(self._pending_step, self._pending)
            self._pending = None
            self._pending_step = None

    def done(self):
        """Mark monitoring as complete. Flushes any pending data."""
        self._flush_pending()
        self._send({
            "type": "display",
            "display_type": "monitor",
            "display_id": self._display_id,
            "action": "done",
        })

    def wrap(self, patterns=None):
        """Context manager that captures stdout and auto-logs metrics.

        Args:
            patterns: Optional list of regex strings with named groups
                      to extract metrics from printed output.
        """
        return _MonitorWrapContext(self, patterns)


class _MonitorWrapContext:
    """Context manager that intercepts stdout and parses numeric patterns."""

    def __init__(self, monitor, patterns=None):
        self._monitor = monitor
        self._custom_patterns = patterns or []
        self._old_stdout = None

    def __enter__(self):
        self._old_stdout = sys.stdout
        sys.stdout = _MonitorTeeWriter(
            sys.stdout,
            self._monitor,
            self._custom_patterns,
        )
        return self._monitor

    def __exit__(self, *exc):
        sys.stdout = self._old_stdout
        self._monitor._flush_pending()
        return False


_NUMERIC_RE = re.compile(
    r"(\b[a-zA-Z_][\w]*)\s*(?:[:=]\s*|\s+)"
    r"([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)"
)
_STEP_RE = re.compile(
    r"(?:epoch|step|iter(?:ation)?)\s*[:=]?\s*(\d+)(?:\s*/\s*\d+)?",
    re.IGNORECASE,
)
# Separate regex for the `N/M` denominator so PatternLocker can seed
# a chart's total_steps from the first matched line.
_STEP_TOTAL_RE = re.compile(
    r"(?:epoch|step|iter(?:ation)?)\s*[:=]?\s*\d+\s*/\s*(\d+)",
    re.IGNORECASE,
)


class _MonitorTeeWriter:
    """Wraps stdout: forwards all output AND parses lines for metrics."""

    def __init__(self, underlying, monitor, custom_patterns):
        self._underlying = underlying
        self._monitor = monitor
        self._custom = [
            re.compile(p) if isinstance(p, str) else p
            for p in custom_patterns
        ]
        self._buf = ""

    def write(self, text: str) -> int:
        self._underlying.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._parse_line(line)
        return len(text)

    def _parse_line(self, line: str) -> None:
        step, metrics = _extract_metrics(line, self._custom)
        if metrics:
            self._monitor.log(step=step, **metrics)


_STEP_KEYS = {"epoch", "step", "iter", "iteration"}


def _apply_custom_patterns(line, custom_patterns, metrics):
    """Apply user-provided regex patterns to extract named groups."""
    for pat in custom_patterns:
        m = pat.search(line)
        if m:
            for k, v in m.groupdict().items():
                try:
                    metrics[k] = float(v)
                except (ValueError, TypeError):
                    pass


def _extract_metrics(line, custom_patterns):
    """Parse a line for step and numeric key=value pairs."""
    metrics: dict = {}
    step = None

    m = _STEP_RE.search(line)
    if m:
        step = int(m.group(1))

    for key, val in _NUMERIC_RE.findall(line):
        if key.lower() in _STEP_KEYS:
            if step is None:
                step = int(float(val))
            continue
        try:
            metrics[key] = float(val)
        except ValueError:
            pass

    _apply_custom_patterns(line, custom_patterns, metrics)
    return step, metrics

    def flush(self) -> None:
        self._underlying.flush()

    def fileno(self) -> int:
        raise OSError("_MonitorTeeWriter has no fileno")


class PatternLocker:
    """Auto-detect training metrics in stdout and render a live chart.

    Fed one stdout chunk at a time via feed(). Uses the same regex
    parser as Monitor (_extract_metrics). Locks onto a pattern after
    three consecutive matching lines with the same metric keyset,
    strictly-increasing step, and at least one non-step value that
    varies across the three. On lock, emits an init + three updates
    carrying the buffered points so the chart starts with no gaps.

    Scope: one run per execution. On cell end, finish() emits a done
    message if the locker locked. Multi-stage runs, custom titles,
    and custom regex patterns are covered by the explicit Monitor
    class, which remains available as an escape hatch.
    """

    _counter = 0

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._state = "scanning"  # "scanning" | "locked"
        self._buffered: list[tuple[int, dict]] = []
        self._line_buf = ""
        self._committed_keys: frozenset | None = None
        self._last_step: int | None = None
        self._display_id: str | None = None
        self._title = "Training"
        self._total_steps: int | None = None

    def feed(self, text: str) -> None:
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._process_line(line)

    def finish(self) -> None:
        if self._state == "locked":
            send_message(self._conn, {
                "type": "display",
                "display_type": "monitor",
                "display_id": self._display_id,
                "action": "done",
            })

    def _process_line(self, line: str) -> None:
        step, metrics = _extract_metrics(line, [])
        if step is None or not metrics:
            return
        keyset = frozenset(metrics.keys())
        if self._state == "scanning":
            self._handle_scanning(line, step, metrics, keyset)
        else:
            self._handle_locked(step, metrics, keyset)

    def _handle_scanning(self, line, step, metrics, keyset) -> None:
        if not self._buffered:
            self._buffered.append((step, metrics))
            self._title = self._derive_title(line)
            self._total_steps = self._derive_total(line)
            return

        prev_step, prev_metrics = self._buffered[-1]
        if keyset != frozenset(prev_metrics.keys()) or step <= prev_step:
            # Continuity broken — restart scanning from this line.
            self._buffered = [(step, metrics)]
            self._title = self._derive_title(line)
            self._total_steps = self._derive_total(line)
            return

        self._buffered.append((step, metrics))
        if len(self._buffered) < 3:
            return

        # Require at least one metric to actually vary across the
        # buffered points. Constant values are usually status/heartbeat
        # output rather than training.
        varies = any(
            len({m[k] for _, m in self._buffered}) > 1
            for k in keyset
        )
        if not varies:
            self._buffered.pop(0)
            return

        self._lock(keyset)

    def _handle_locked(self, step, metrics, keyset) -> None:
        if keyset != self._committed_keys:
            return
        if self._last_step is not None and step <= self._last_step:
            return
        self._last_step = step
        self._emit_update(step, metrics)

    def _lock(self, keyset) -> None:
        self._state = "locked"
        self._committed_keys = keyset
        PatternLocker._counter += 1
        self._display_id = f"auto_{PatternLocker._counter}_{id(self):x}"

        send_message(self._conn, {
            "type": "display",
            "display_type": "monitor",
            "display_id": self._display_id,
            "action": "init",
            "config": {
                "title": self._title,
                "total_steps": self._total_steps,
            },
        })

        for step, metrics in self._buffered:
            self._last_step = step
            self._emit_update(step, metrics)
        self._buffered = []

    def _emit_update(self, step, metrics) -> None:
        send_message(self._conn, {
            "type": "display",
            "display_type": "monitor",
            "display_id": self._display_id,
            "action": "update",
            "step": step,
            "metrics": metrics,
            "ts": time.time(),
        })

    @staticmethod
    def _derive_title(line: str) -> str:
        m = _STEP_RE.search(line)
        if m is None:
            return "Training"
        prefix = line[:m.start()].strip().rstrip("|,:-").strip()
        return prefix or "Training"

    @staticmethod
    def _derive_total(line: str) -> int | None:
        m = _STEP_TOTAL_RE.search(line)
        return int(m.group(1)) if m else None


def execute_code(
    conn: socket.socket, code: str, notebook_id: str = "",
) -> bool:
    """Run user code. Returns True if an exception was raised (including
    KeyboardInterrupt from an interrupt), False on clean completion."""
    global _current_conn, _exec_baseline_thread_ids
    _current_conn = conn
    # Snapshot live thread ids before user code runs. Any thread still
    # alive on interrupt whose id is NOT in this set was spawned by user
    # code and is fair game for the interrupt cascade.
    _exec_baseline_thread_ids = {
        t.ident for t in threading.enumerate() if t.ident is not None
    }
    ns = _get_namespace(notebook_id)
    ns["display_audio"] = display_audio
    ns["Monitor"] = Monitor
    _invalidate_user_modules()

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    errored = False

    try:
        sys.stdout = StreamingWriter(conn, "stdout")
        sys.stderr = StreamingWriter(conn, "stderr")

        # Try eval first for expressions, fall back to exec
        try:
            result = eval(code, ns)  # noqa: S307
            if result is not None:
                send_message(conn, {
                    "type": "result", "data": repr(result),
                })
        except SyntaxError:
            exec(code, ns)  # noqa: S102

    except KeyboardInterrupt:
        errored = True
        send_message(conn, {
            "type": "error",
            "ename": "KeyboardInterrupt",
            "evalue": "",
            "traceback": ["KeyboardInterrupt"],
        })
    except Exception as e:
        errored = True
        send_message(conn, {
            "type": "error",
            "ename": type(e).__name__,
            "evalue": str(e),
            "traceback": traceback.format_exception(e),
        })
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return errored


def handle_message(conn: socket.socket, msg: dict) -> None:
    global _current_locker
    msg_type = msg.get("type")
    if msg_type == "execute":
        code = msg.get("code", "")
        notebook_id = msg.get("notebook_id", "")
        _current_locker = PatternLocker(conn)
        try:
            send_message(conn, {"type": "state", "execution_state": "running"})

            errored = False
            if is_pip_command(code):
                argv = _build_uv_command(code)
                if argv is None:
                    send_message(conn, {
                        "type": "error",
                        "ename": "ParseError",
                        "evalue": f"Unrecognized install command: {code.strip()!r}",
                        "traceback": [],
                    })
                    errored = True
                else:
                    errored = execute_uv(conn, argv)
            elif is_requirements_block(code):
                errored = execute_uv(conn, requirements_to_uv_add(code))
            elif is_apt_command(code):
                errored = execute_apt(conn, code)
            elif is_shell_command(code):
                execute_shell(conn, code[1:].strip())
            else:
                errored = execute_code(conn, code, notebook_id)

            created = _drain_created_dirs()
            if created:
                send_message(conn, {"type": "created_dirs", "dirs": created})

            _current_locker.finish()
            final_state = "errored" if errored else "completed"
            send_message(conn, {"type": "state", "execution_state": final_state})
        finally:
            _current_locker = None

    elif msg_type == "ping":
        send_message(conn, {"type": "pong"})


def _set_async_exc(tid: int, exc=KeyboardInterrupt) -> None:
    """Post an async exception to a Python thread by ident.

    This is the only reliable way to interrupt a non-main CPython
    thread. The exception fires at the next Python bytecode boundary
    in that thread — stuck C extension calls still need to return
    before it takes effect.
    """
    import ctypes
    if tid is None:
        return
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(exc),
    )
    if res > 1:
        # Defensive: unwind if more than one thread was affected.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), None,
        )


def _interrupt_targets(exec_thread) -> list[int]:
    """Return (exec_thread, plus any threads spawned during this run)."""
    targets = []
    if exec_thread.ident is not None:
        targets.append(exec_thread.ident)
    for t in threading.enumerate():
        tid = t.ident
        if tid is None or tid == exec_thread.ident:
            continue
        if tid in _exec_baseline_thread_ids:
            continue
        targets.append(tid)
    return targets


def _start_interrupt_cascade(exec_thread, timeout_s: float = 5.0) -> None:
    """Repeatedly post KeyboardInterrupt to the exec thread and any
    threads user code spawned during this run. Runs as a daemon so
    the client message loop stays responsive for force_stop.

    This is a cooperative interrupt: the async exception fires at
    the next Python bytecode boundary in the target thread. If the
    thread is stuck in a long C call (nanosleep, a big torch kernel,
    queue.get on a lock), the exception stays pending until the C
    call returns. Users can escalate to Force Stop if the wait is
    unacceptable.

    Note: we deliberately do NOT pthread_kill with SIGINT. CPython
    handles signals only on the main thread, so a worker's nanosleep
    is not woken by SIGINT, and worse, the signal may be picked up
    by the executor's main accept() loop and crash the server.
    """
    def _cascade() -> None:
        deadline = time.monotonic() + timeout_s
        while exec_thread.is_alive() and time.monotonic() < deadline:
            for tid in _interrupt_targets(exec_thread):
                _set_async_exc(tid)
            time.sleep(0.2)

    threading.Thread(target=_cascade, daemon=True).start()


def _dispatch_msg(conn, msg, exec_thread):
    """Route a parsed message, returning the (possibly new) exec_thread."""
    global current_thread
    msg_type = msg.get("type")

    if msg_type == "interrupt":
        if exec_thread and exec_thread.is_alive():
            _start_interrupt_cascade(exec_thread)
        return exec_thread

    if msg_type == "execute":
        if exec_thread and exec_thread.is_alive():
            exec_thread.join()
        t = threading.Thread(
            target=handle_message, args=(conn, msg), daemon=True,
        )
        current_thread = t
        t.start()
        return t

    handle_message(conn, msg)
    return exec_thread


def handle_client(conn: socket.socket, addr: tuple) -> None:
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buffer = ""
    exec_thread = None

    try:
        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:
                break
            buffer += data.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    exec_thread = _dispatch_msg(
                        conn, json.loads(line), exec_thread,
                    )
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        if exec_thread and exec_thread.is_alive():
            exec_thread.join(timeout=2)
        conn.close()


def main() -> None:
    # Set CWD so relative paths from user code land in /env/files
    _original_os_makedirs("/env/files", exist_ok=True)
    os.chdir("/env/files")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"Executor server listening on {HOST}:{PORT}", flush=True)

    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        thread.start()


if __name__ == "__main__":
    main()
