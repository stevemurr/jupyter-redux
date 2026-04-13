"""WebSocket handlers for real-time cell execution.

Cell executions are owned by a process-global ExecutionManager so they survive
WebSocket disconnects. Each in-flight execution holds a buffer of all messages
produced; reconnecting clients atomically subscribe + replay so they see
existing output and continue receiving live messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from src.app import app
from src.config import settings
from src.container import ContainerService
from src.environment import EnvironmentService
from src.executor import ExecutorClient
from src.metrics import get_host_cpu_pct, get_host_memory
from src.models import ContainerStatus, ExecutionState, Output, OutputType

logger = logging.getLogger(__name__)

env_service = EnvironmentService()
container_service = ContainerService()

# Per-notebook execution state — preserved across WS reconnects
execution_counts: dict[str, int] = {}
execution_locks: dict[str, asyncio.Lock] = {}

# Per-environment executor — shared across notebooks in the same env.
# Refcount = (# active WS connections) + (# in-flight executions)
active_executors: dict[str, ExecutorClient] = {}
_executor_refcounts: dict[str, int] = {}

# Per-environment resource broadcaster. One poll task per env fans out
# stats to all currently connected WebSockets for that env; tasks are
# started lazily on first subscribe and cancelled on last unsubscribe.
_resource_subscribers: dict[str, set[WebSocket]] = {}
_resource_tasks: dict[str, asyncio.Task] = {}
_RESOURCE_POLL_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# ExecutionManager: tracks in-flight executions independent of WS lifetime
# ---------------------------------------------------------------------------


@dataclass
class RunningExecution:
    notebook_id: str
    environment_id: str
    cell_id: str
    execution_count: int
    started_at: float
    buffer: list[dict] = field(default_factory=list)
    persisted_outputs: list[Output] = field(default_factory=list)
    final_state: str | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)


class ExecutionManager:
    """Process-global manager of in-flight notebook executions."""

    def __init__(self) -> None:
        self._by_notebook: dict[str, RunningExecution] = {}

    def get(self, notebook_id: str) -> RunningExecution | None:
        return self._by_notebook.get(notebook_id)

    def register(self, rec: RunningExecution) -> None:
        self._by_notebook[rec.notebook_id] = rec

    def unregister(self, notebook_id: str) -> None:
        self._by_notebook.pop(notebook_id, None)

    def append(self, notebook_id: str, msg: dict) -> None:
        """Append a message to the buffer and broadcast to subscribers.

        Both operations are sync (no awaits) so they are atomic relative
        to ``subscribe`` running on the same event loop.
        """
        rec = self._by_notebook.get(notebook_id)
        if rec is None:
            return
        rec.buffer.append(msg)
        for q in list(rec.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(
        self, notebook_id: str,
    ) -> tuple[asyncio.Queue, list[dict]] | None:
        """Atomically snapshot the buffer and add a subscriber queue.

        Returns (queue, replay_messages) so the caller can first send the
        replay snapshot then drain the queue for live updates. The snapshot
        and the subscriber registration happen with no awaits between them,
        which guarantees no message is delivered twice or dropped.
        """
        rec = self._by_notebook.get(notebook_id)
        if rec is None:
            return None
        q: asyncio.Queue = asyncio.Queue()
        replay = list(rec.buffer)
        rec.subscribers.add(q)
        return q, replay

    def has_inflight_for_env(self, environment_id: str) -> bool:
        for rec in self._by_notebook.values():
            if rec.environment_id == environment_id and rec.final_state is None:
                return True
        return False


exec_manager = ExecutionManager()


async def send_json(ws: WebSocket, data: dict) -> None:
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Executor refcount helpers
# ---------------------------------------------------------------------------


def _acquire_executor_ref(environment_id: str) -> None:
    _executor_refcounts[environment_id] = (
        _executor_refcounts.get(environment_id, 0) + 1
    )


async def _safe_disconnect(executor: ExecutorClient) -> None:
    try:
        await executor.disconnect()
    except Exception:
        pass


def _release_executor_ref(environment_id: str) -> None:
    """Decrement the executor refcount; tear down if it reaches zero.

    Sync so it can be called from sync contexts including done callbacks.
    The actual disconnect runs as a fire-and-forget task.
    """
    count = _executor_refcounts.get(environment_id, 1) - 1
    if count <= 0:
        executor = active_executors.pop(environment_id, None)
        _executor_refcounts.pop(environment_id, None)
        if executor:
            asyncio.create_task(_safe_disconnect(executor))
    else:
        _executor_refcounts[environment_id] = count


async def _handle_force_stop(
    ws: WebSocket,
    notebook_id: str,
    environment_id: str,
    executor: ExecutorClient,
) -> None:
    """Hard-kill the env container and tear down this WS.

    Steps:
    1. Broadcast a terminal "errored" state for any in-flight cell so
       subscribers stop showing "stopping" forever.
    2. Run docker restart on the env container (SIGKILL + start) in a
       thread since it blocks for a couple of seconds.
    3. Clear the env's entry from active_executors / refcounts and
       discard the old (now-broken) ExecutorClient.
    4. Close this WebSocket; the browser's auto-reconnect will walk
       through _setup_connection again and build a fresh executor.
    """
    print(
        f"[WS] force_stop requested for env {environment_id[:8]}",
        flush=True,
    )

    rec = exec_manager.get(notebook_id)
    if rec is not None and rec.final_state is None:
        exec_manager.append(notebook_id, {
            "type": "error",
            "cell_id": rec.cell_id,
            "ename": "KernelRestart",
            "evalue": "Cell was force-stopped; kernel is restarting.",
            "traceback": [],
        })
        exec_manager.append(notebook_id, {
            "type": "state",
            "cell_id": rec.cell_id,
            "execution_state": "errored",
            "execution_count": rec.execution_count,
        })
        rec.final_state = "errored"
        # Wake subscribers so they exit their forwarder loops.
        for q in list(rec.subscribers):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        exec_manager.unregister(notebook_id)

    await send_json(ws, {
        "type": "container_state",
        "status": "starting",
        "message": "Restarting kernel...",
    })

    try:
        state = await asyncio.to_thread(
            container_service.restart_container, environment_id,
        )
    except Exception:
        logger.exception("force_stop: restart_container failed")
        state = None

    # Discard the stale ExecutorClient for this env regardless of what
    # the restart call returned. Any future execute on this env goes
    # through a fresh _setup_connection.
    old_executor = active_executors.pop(environment_id, None)
    _executor_refcounts.pop(environment_id, None)
    if old_executor is not None:
        asyncio.create_task(_safe_disconnect(old_executor))

    if state and state.status == ContainerStatus.READY:
        await send_json(ws, {
            "type": "container_state",
            "status": "ready",
            "message": "Kernel restarted.",
        })
    else:
        msg = state.error_message if state else "Kernel restart failed"
        await send_json(ws, {
            "type": "container_state",
            "status": "error",
            "message": msg,
        })

    # Close this WS cleanly — the browser will reconnect and pick up
    # a fresh executor via _setup_connection.
    try:
        await ws.close(code=1000, reason="force_stop")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Resource monitor broadcaster
# ---------------------------------------------------------------------------


async def _collect_resource_stats(environment_id: str) -> dict:
    """Collect host-wide CPU/memory and GPU stats.

    CPU and memory are read from /proc (host-wide, reflects everything
    on the machine regardless of container scope). GPU stats still go
    through the env container's nvidia-smi since util is a cross-
    container hardware counter anyway.
    """
    cpu_pct = await asyncio.to_thread(get_host_cpu_pct)
    mem = await asyncio.to_thread(get_host_memory)
    host_stats: dict | None = None
    if mem is not None:
        host_stats = {
            "cpu_pct": round(cpu_pct, 1),
            "mem_used": mem["used"],
            "mem_total": mem["total"],
        }

    env = env_service.get_environment(environment_id)
    gpu_stats = None
    if env and env.gpu:
        gpu_stats = await asyncio.to_thread(
            container_service.get_gpu_stats, environment_id,
        )
    return {
        "type": "resource_stats",
        "container": host_stats,
        "gpu": gpu_stats,
    }


async def _resource_poll_loop(environment_id: str) -> None:
    while True:
        try:
            msg = await _collect_resource_stats(environment_id)
        except Exception:
            logger.exception(
                "resource poll failed for env %s", environment_id[:8],
            )
            msg = {"type": "resource_stats", "container": None, "gpu": None}

        for ws in list(_resource_subscribers.get(environment_id, ())):
            await send_json(ws, msg)

        await asyncio.sleep(_RESOURCE_POLL_INTERVAL)


def _subscribe_resource_stats(environment_id: str, ws: WebSocket) -> None:
    subs = _resource_subscribers.setdefault(environment_id, set())
    subs.add(ws)
    if environment_id not in _resource_tasks:
        task = asyncio.create_task(_resource_poll_loop(environment_id))
        _resource_tasks[environment_id] = task


def _unsubscribe_resource_stats(environment_id: str, ws: WebSocket) -> None:
    subs = _resource_subscribers.get(environment_id)
    if subs is None:
        return
    subs.discard(ws)
    if subs:
        return
    # Last subscriber left — cancel the poller and clean up.
    _resource_subscribers.pop(environment_id, None)
    task = _resource_tasks.pop(environment_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Container image build streaming (unchanged)
# ---------------------------------------------------------------------------


async def _stream_image_build(
    ws: WebSocket, env, tag: str
) -> None:
    log_queue: queue.Queue[str | None] = queue.Queue()

    def _run_build():
        try:
            _, stream = container_service.build_image_streaming(
                env.python_version, env.gpu
            )
            for chunk in stream:
                if "error" in chunk:
                    log_queue.put(f"ERROR: {chunk['error']}")
                    break
                line = chunk.get("stream", "").rstrip()
                if line:
                    log_queue.put(line)
        finally:
            log_queue.put(None)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_build)

    while True:
        await asyncio.sleep(0.3)
        lines, done = _drain_log_queue(log_queue)
        if lines:
            await send_json(ws, {"type": "build_log", "lines": lines})
        if done:
            await send_json(ws, {
                "type": "container_state",
                "status": "starting",
                "message": f"Image {tag} built. Starting container...",
            })
            return


def _drain_log_queue(
    log_queue: queue.Queue,
) -> tuple[list[str], bool]:
    lines: list[str] = []
    done = False
    while True:
        try:
            line = log_queue.get_nowait()
        except queue.Empty:
            break
        if line is None:
            done = True
            break
        lines.append(line)
    return lines, done


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------


async def _setup_connection(
    ws: WebSocket, notebook_id: str
) -> tuple[ExecutorClient | None, str]:
    """Validate notebook, start container, connect executor.

    Returns (executor, environment_id) on success, (None, "") on failure.
    """
    notebook = env_service.get_notebook(notebook_id)
    if notebook is None:
        await ws.close(code=4004, reason="Notebook not found")
        return None, ""

    environment_id = notebook.environment_id
    env = env_service.get_environment(environment_id)
    if env is None:
        await ws.close(code=4004, reason="Environment not found")
        return None, ""

    # Reuse existing executor for this environment if available
    existing = active_executors.get(environment_id)
    if existing is not None:
        _acquire_executor_ref(environment_id)
        return existing, environment_id

    # Check if the image needs building
    tag = container_service.get_image_tag(env.python_version, env.gpu)
    if not container_service.has_image(tag):
        await send_json(ws, {
            "type": "container_state",
            "status": "building",
            "message": f"Building image {tag}...",
        })
        try:
            await _stream_image_build(ws, env, tag)
        except Exception as e:
            await send_json(ws, {
                "type": "container_state",
                "status": "error",
                "message": f"Image build failed: {e}",
            })
            await ws.close(code=4010, reason="Image build failed")
            return None, ""

    # Start container (image now exists)
    await send_json(ws, {
        "type": "container_state",
        "status": "starting",
        "message": f"Starting container (image {tag})...",
    })
    state = container_service.start_container(
        environment_id,
        python_version=env.python_version,
        gpu=env.gpu,
    )
    await send_json(ws, {
        "type": "container_state",
        "status": state.status.value,
        "message": state.error_message,
    })

    if state.status != ContainerStatus.READY:
        await ws.close(code=4010, reason="Container failed to start")
        return None, ""

    if settings.docker_network:
        exec_host = container_service.get_container_name(environment_id)
        exec_port = settings.executor_port
    else:
        exec_host = "127.0.0.1"
        exec_port = state.host_port or settings.executor_port

    executor = ExecutorClient(host=exec_host, port=exec_port)
    try:
        await executor.connect()
    except ConnectionError as e:
        await send_json(ws, {
            "type": "container_state",
            "status": "error",
            "message": str(e),
        })
        await ws.close(code=4010, reason="Cannot connect to executor")
        return None, ""

    active_executors[environment_id] = executor
    _executor_refcounts[environment_id] = 1
    return executor, environment_id


# ---------------------------------------------------------------------------
# WebSocket entry point
# ---------------------------------------------------------------------------


@app.websocket("/ws/notebooks/{notebook_id}")
async def notebook_websocket(
    ws: WebSocket, notebook_id: str
) -> None:
    await ws.accept()
    print(f"[WS] accepted for notebook {notebook_id[:8]}", flush=True)

    executor, environment_id = await _setup_connection(ws, notebook_id)
    if executor is None:
        print("[WS] setup_connection failed", flush=True)
        return

    print(
        f"[WS] executor connected (env {environment_id[:8]})",
        flush=True,
    )

    execution_locks.setdefault(notebook_id, asyncio.Lock())
    execution_counts.setdefault(notebook_id, 0)

    # WS-local subscription tasks: cancelled on disconnect, separate from
    # the manager-owned execution tasks (which keep running).
    ws_local_tasks: set[asyncio.Task] = set()

    # Subscribe to per-environment resource stats (starts the poller if
    # this is the first subscriber).
    _subscribe_resource_stats(environment_id, ws)

    # If there's an in-flight execution for this notebook, attach to it
    if exec_manager.get(notebook_id) is not None:
        print(
            f"[WS] reattaching to in-flight execution for {notebook_id[:8]}",
            flush=True,
        )
        t = asyncio.create_task(_subscribe_and_forward(ws, notebook_id))
        ws_local_tasks.add(t)
        t.add_done_callback(ws_local_tasks.discard)

    try:
        await _message_loop(
            ws, notebook_id, environment_id, executor, ws_local_tasks,
        )
    except WebSocketDisconnect:
        print(f"[WS] client disconnected {notebook_id[:8]}", flush=True)
    except Exception:
        logger.exception("WebSocket error for notebook %s", notebook_id)
    finally:
        _unsubscribe_resource_stats(environment_id, ws)
        # Cancel WS-local subscription tasks (NOT the execution tasks)
        for t in list(ws_local_tasks):
            if not t.done():
                t.cancel()
        _release_executor_ref(environment_id)


async def _message_loop(
    ws: WebSocket,
    notebook_id: str,
    environment_id: str,
    executor: ExecutorClient,
    ws_local_tasks: set[asyncio.Task],
) -> None:
    while True:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "execute":
            cell_id = msg.get("cell_id", "")
            code = msg.get("code", "")
            print(f"[WS] execute received: {cell_id[:8]}", flush=True)
            container_service.record_activity(environment_id)

            # Acquire the executor ref SYNCHRONOUSLY before scheduling the
            # task. Otherwise the WS could disconnect and tear down the
            # executor before the task gets a chance to run, leaving the
            # task with a dangling executor reference.
            _acquire_executor_ref(environment_id)
            exec_task = asyncio.create_task(
                _execute_loop(
                    notebook_id, environment_id, cell_id, code, executor,
                )
            )
            exec_task.add_done_callback(
                lambda _t, eid=environment_id: _release_executor_ref(eid)
            )

            # Spawn a WS-local subscription task that forwards messages
            # for this specific execution to the current WS.
            t = asyncio.create_task(
                _subscribe_and_forward(
                    ws, notebook_id, expect_cell_id=cell_id,
                )
            )
            ws_local_tasks.add(t)
            t.add_done_callback(ws_local_tasks.discard)

        elif msg_type == "interrupt":
            # Broadcast "stopping" state immediately so all subscribers
            # see the transition without waiting for the executor to
            # actually post an exception — C-bound calls can keep the
            # real termination several seconds away.
            rec = exec_manager.get(notebook_id)
            if rec is not None and rec.final_state is None:
                exec_manager.append(notebook_id, {
                    "type": "state",
                    "cell_id": rec.cell_id,
                    "execution_state": "stopping",
                    "execution_count": rec.execution_count,
                })
            try:
                await executor.interrupt()
            except Exception:
                logger.warning(
                    "Failed to send interrupt for %s",
                    msg.get("cell_id", ""),
                )

        elif msg_type == "force_stop":
            await _handle_force_stop(
                ws, notebook_id, environment_id, executor,
            )


# ---------------------------------------------------------------------------
# Execution loop (manager-owned, survives WS disconnect)
# ---------------------------------------------------------------------------


async def _execute_loop(
    notebook_id: str,
    environment_id: str,
    cell_id: str,
    code: str,
    executor: ExecutorClient,
) -> None:
    """Run a cell. Owned by ExecutionManager, not the WebSocket.

    The caller (message_loop) is responsible for acquiring the executor ref
    before scheduling this task and releasing it via a done callback.
    """
    await _execute_under_lock(
        notebook_id, environment_id, cell_id, code, executor,
    )


async def _execute_under_lock(
    notebook_id: str,
    environment_id: str,
    cell_id: str,
    code: str,
    executor: ExecutorClient,
) -> None:
    lock = execution_locks[notebook_id]
    print(f"[exec] waiting for lock {cell_id[:8]}", flush=True)

    async with lock:
        print(f"[exec] lock acquired, executing {cell_id[:8]}", flush=True)
        execution_counts[notebook_id] += 1
        exec_count = execution_counts[notebook_id]

        rec = RunningExecution(
            notebook_id=notebook_id,
            environment_id=environment_id,
            cell_id=cell_id,
            execution_count=exec_count,
            started_at=time.time(),
        )
        exec_manager.register(rec)

        # Initial state broadcast — first message in the replay buffer
        exec_manager.append(notebook_id, {
            "type": "state",
            "cell_id": cell_id,
            "execution_state": "running",
            "execution_count": exec_count,
        })

        notebook = env_service.get_notebook(notebook_id)
        _set_cell_running(notebook, cell_id, exec_count)

        final_state = "completed"
        try:
            async for msg in executor.execute(code, notebook_id):
                terminal = await _process_executor_msg_into_manager(rec, msg)
                if terminal:
                    final_state = terminal
        except asyncio.CancelledError:
            final_state = "errored"
            raise
        except Exception as e:
            logger.exception("Execution error for cell %s", cell_id)
            final_state = "errored"
            exec_manager.append(notebook_id, {
                "type": "error",
                "cell_id": cell_id,
                "ename": "ExecutionError",
                "evalue": str(e),
                "traceback": [],
            })
        finally:
            _finalize_execution(
                rec, notebook, notebook_id, cell_id, exec_count, final_state,
            )


def _finalize_execution(
    rec: RunningExecution,
    notebook,
    notebook_id: str,
    cell_id: str,
    exec_count: int,
    final_state: str,
) -> None:
    rec.final_state = final_state
    print(f"[exec] done: {final_state} {cell_id[:8]}", flush=True)

    exec_manager.append(notebook_id, {
        "type": "state",
        "cell_id": cell_id,
        "execution_state": final_state,
        "execution_count": exec_count,
    })

    _persist_cell_outputs(
        notebook, notebook_id, cell_id,
        final_state, exec_count, rec.persisted_outputs,
    )

    # Send sentinel to all subscribers so they exit cleanly
    for q in list(rec.subscribers):
        try:
            q.put_nowait(None)
        except Exception:
            pass

    exec_manager.unregister(notebook_id)


def _record_output_msg(rec: RunningExecution, msg: dict) -> None:
    text = msg.get("text", "")
    if text.strip():
        print(f"[WS] relay: {text[:60].strip()}", flush=True)
    exec_manager.append(rec.notebook_id, {
        "type": "output",
        "cell_id": rec.cell_id,
        "stream": msg.get("stream", "stdout"),
        "text": text,
    })
    rec.persisted_outputs.append(Output(
        output_type=OutputType(msg.get("stream", "stdout")),
        content=text,
    ))


def _record_result_msg(rec: RunningExecution, msg: dict) -> None:
    exec_manager.append(rec.notebook_id, {
        "type": "result",
        "cell_id": rec.cell_id,
        "data": msg.get("data", ""),
    })
    rec.persisted_outputs.append(Output(
        output_type=OutputType.RESULT,
        content=msg.get("data", ""),
    ))


def _record_display_msg(rec: RunningExecution, msg: dict) -> None:
    fwd = {"type": "display", "cell_id": rec.cell_id}
    for k, v in msg.items():
        if k != "type":
            fwd[k] = v
    print(
        f"[WS] display: {msg.get('display_type', '')} "
        f"{msg.get('action', '')}",
        flush=True,
    )
    exec_manager.append(rec.notebook_id, fwd)


def _record_error_msg(rec: RunningExecution, msg: dict) -> None:
    exec_manager.append(rec.notebook_id, {
        "type": "error",
        "cell_id": rec.cell_id,
        "ename": msg.get("ename", ""),
        "evalue": msg.get("evalue", ""),
        "traceback": msg.get("traceback", []),
    })
    rec.persisted_outputs.append(Output(
        output_type=OutputType.ERROR,
        content=f"{msg.get('ename', '')}: {msg.get('evalue', '')}",
    ))


def _record_created_dirs_msg(rec: RunningExecution, msg: dict) -> None:
    dirs = msg.get("dirs", [])
    if dirs:
        print(f"[WS] created_dirs: {dirs}", flush=True)
        exec_manager.append(rec.notebook_id, {
            "type": "created_dirs",
            "cell_id": rec.cell_id,
            "dirs": dirs,
        })


_MSG_HANDLERS = {
    "output": _record_output_msg,
    "result": _record_result_msg,
    "display": _record_display_msg,
    "created_dirs": _record_created_dirs_msg,
}


async def _process_executor_msg_into_manager(
    rec: RunningExecution, msg: dict,
) -> str | None:
    """Forward an executor message into the manager broadcast and update
    the persistence buffer. Returns 'completed'/'errored' on terminal state.
    """
    msg_type = msg.get("type")

    handler = _MSG_HANDLERS.get(msg_type or "")
    if handler is not None:
        handler(rec, msg)
        return None

    if msg_type == "error":
        _record_error_msg(rec, msg)
        return "errored"

    if msg_type == "state":
        es = msg.get("execution_state", "")
        if es in ("completed", "errored"):
            return es

    return None


# ---------------------------------------------------------------------------
# Per-WebSocket subscription pump
# ---------------------------------------------------------------------------


async def _wait_for_execution(
    notebook_id: str, expect_cell_id: str | None,
) -> RunningExecution | None:
    """Poll until the manager has an in-flight execution matching the
    expected cell_id (or any cell, if not specified). Returns None on
    timeout. Generous timeout because the lock may be queued behind a
    long previous execution.
    """
    deadline = time.time() + 600.0
    while time.time() < deadline:
        rec = exec_manager.get(notebook_id)
        if rec is not None and (
            expect_cell_id is None or rec.cell_id == expect_cell_id
        ):
            return rec
        await asyncio.sleep(0.05)
    return None


async def _subscribe_and_forward(
    ws: WebSocket,
    notebook_id: str,
    expect_cell_id: str | None = None,
) -> None:
    """Forward messages from an in-flight execution to the WebSocket."""
    rec = await _wait_for_execution(notebook_id, expect_cell_id)
    if rec is None:
        return

    sub = exec_manager.subscribe(notebook_id)
    if sub is None:
        return
    q, replay = sub

    try:
        for buffered in replay:
            await send_json(ws, buffered)
        if rec.final_state is not None:
            return
        while True:
            next_msg = await q.get()
            if next_msg is None:  # sentinel: execution finished
                break
            await send_json(ws, next_msg)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("subscription forwarder failed")
    finally:
        rec.subscribers.discard(q)


# ---------------------------------------------------------------------------
# Notebook persistence helpers (unchanged)
# ---------------------------------------------------------------------------


def _set_cell_running(notebook, cell_id: str, exec_count: int) -> None:
    if not notebook:
        return
    for cell in notebook.cells:
        if cell.id == cell_id:
            cell.execution_state = ExecutionState.RUNNING
            cell.execution_count = exec_count
            cell.outputs = []
            break


def _persist_cell_outputs(
    notebook, notebook_id: str, cell_id: str,
    final_state: str, exec_count: int, outputs: list[Output],
) -> None:
    if not notebook:
        return
    for cell in notebook.cells:
        if cell.id == cell_id:
            cell.execution_state = ExecutionState(final_state)
            cell.execution_count = exec_count
            cell.outputs = outputs
            break
    env_service.save_notebook(notebook)
