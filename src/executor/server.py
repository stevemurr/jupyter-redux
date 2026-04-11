"""TCP execution server that runs inside each notebook container.

Listens on a TCP port, maintains a persistent Python namespace,
executes submitted code, and streams stdout/stderr back to the client.
"""

import base64
import json
import mimetypes
import os
import re
import signal
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


def _get_namespace(notebook_id: str = "") -> dict:
    """Return the namespace for a notebook, creating it if needed."""
    key = notebook_id or "__default__"
    if key not in namespaces:
        namespaces[key] = {}
    return namespaces[key]


def send_message(conn: socket.socket, msg: dict) -> None:
    data = json.dumps(msg) + "\n"
    conn.sendall(data.encode("utf-8"))


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
    """File-like object that sends each write() over the socket immediately."""

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

    def fileno(self) -> int:
        raise OSError("StreamingWriter has no fileno")


def is_pip_command(code: str) -> bool:
    stripped = code.strip()
    return stripped.startswith(("pip install", "pip uninstall", "pip list", "pip show"))


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


PIP_TARGET_DIR = "/env/lib"


def requirements_to_pip_args(code: str) -> list[str]:
    """Convert requirements.txt content to pip install arg list."""
    pkgs = []
    for line in code.strip().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            pkgs.append(stripped)
    return [
        "python", "-m", "pip", "install",
        "--target", PIP_TARGET_DIR, *pkgs,
    ]


def is_shell_command(code: str) -> bool:
    return code.strip().startswith("!")


def execute_pip(conn: socket.socket, args: list[str]) -> None:
    """Run pip as a list of args (no shell interpretation)."""
    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            args,
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
    """Re-process .pth files from site-packages so editable installs
    done after the executor started are visible on sys.path."""
    import glob
    import site

    for sp_dir in site.getsitepackages():
        for pth in glob.glob(os.path.join(sp_dir, "*.pth")):
            try:
                for line in open(pth):
                    line = line.strip()
                    if line and not line.startswith("#") and os.path.isdir(line):
                        if line not in sys.path:
                            sys.path.insert(0, line)
            except OSError:
                pass


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
        """Log metrics for one step. Auto-increments step if omitted."""
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


def execute_code(
    conn: socket.socket, code: str, notebook_id: str = "",
) -> None:
    global _current_conn
    _current_conn = conn
    ns = _get_namespace(notebook_id)
    ns["display_audio"] = display_audio
    ns["Monitor"] = Monitor
    _invalidate_user_modules()

    old_stdout = sys.stdout
    old_stderr = sys.stderr

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
        send_message(conn, {
            "type": "error",
            "ename": "KeyboardInterrupt",
            "evalue": "",
            "traceback": ["KeyboardInterrupt"],
        })
    except Exception as e:
        send_message(conn, {
            "type": "error",
            "ename": type(e).__name__,
            "evalue": str(e),
            "traceback": traceback.format_exception(e),
        })
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def handle_message(conn: socket.socket, msg: dict) -> None:
    msg_type = msg.get("type")
    if msg_type == "execute":
        code = msg.get("code", "")
        notebook_id = msg.get("notebook_id", "")
        send_message(conn, {"type": "state", "execution_state": "running"})

        if is_pip_command(code):
            stripped = code.strip()
            if stripped.startswith("pip install"):
                pkgs = stripped[len("pip install"):].split()
                execute_pip(conn, [
                    "python", "-m", "pip", "install",
                    "--target", PIP_TARGET_DIR, *pkgs,
                ])
            else:
                # pip uninstall, pip list, pip show — no --target
                execute_pip(conn, ["python", "-m", *stripped.split()])
        elif is_requirements_block(code):
            execute_pip(conn, requirements_to_pip_args(code))
        elif is_shell_command(code):
            execute_shell(conn, code[1:].strip())
        else:
            execute_code(conn, code, notebook_id)

        send_message(conn, {"type": "state", "execution_state": "completed"})

    elif msg_type == "interrupt":
        if current_thread and current_thread.is_alive():
            signal.raise_signal(signal.SIGINT)

    elif msg_type == "ping":
        send_message(conn, {"type": "pong"})


def handle_client(conn: socket.socket, addr: tuple) -> None:
    global current_thread
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buffer = ""
    try:
        while True:
            data = conn.recv(BUFFER_SIZE)
            if not data:
                break
            buffer += data.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    msg = json.loads(line)
                    current_thread = threading.current_thread()
                    handle_message(conn, msg)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()


def main() -> None:
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
