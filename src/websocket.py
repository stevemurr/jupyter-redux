"""WebSocket handlers for real-time cell execution."""

from __future__ import annotations

import asyncio
import json
import logging
import queue

from fastapi import WebSocket, WebSocketDisconnect

from src.app import app
from src.config import settings
from src.container import ContainerService
from src.executor import ExecutorClient
from src.models import ContainerStatus, ExecutionState, Output, OutputType
from src.notebook import NotebookService

logger = logging.getLogger(__name__)

notebook_service = NotebookService()
container_service = ContainerService()

# Per-notebook state — reset on each new WebSocket connection
execution_counts: dict[str, int] = {}
active_executors: dict[str, ExecutorClient] = {}
execution_locks: dict[str, asyncio.Lock] = {}
# Track pending execute tasks so we can cancel them on reconnect
_pending_tasks: dict[str, set[asyncio.Task]] = {}


async def send_json(ws: WebSocket, data: dict) -> None:
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


def _cancel_stale_tasks(notebook_id: str) -> None:
    """Cancel any leftover execute tasks from a previous connection."""
    tasks = _pending_tasks.pop(notebook_id, set())
    for task in tasks:
        if not task.done():
            task.cancel()


async def _stream_image_build(
    ws: WebSocket, notebook, tag: str
) -> None:
    """Build a Docker image in a thread, streaming log lines to the client."""
    log_queue: queue.Queue[str | None] = queue.Queue()

    def _run_build():
        try:
            _, stream = container_service.build_image_streaming(
                notebook.python_version, notebook.gpu
            )
            for chunk in stream:
                if "error" in chunk:
                    log_queue.put(f"ERROR: {chunk['error']}")
                    break
                line = chunk.get("stream", "").rstrip()
                if line:
                    log_queue.put(line)
        finally:
            log_queue.put(None)  # Sentinel: build done

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
    """Drain all available lines from the queue.
    Returns (lines, is_done).
    """
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


async def _setup_connection(
    ws: WebSocket, notebook_id: str
) -> ExecutorClient | None:
    """Validate notebook, start container, connect executor.

    If the executor image doesn't exist, builds it and streams the
    build log to the client over the WebSocket.
    Returns executor on success, None on failure (ws closed).
    """
    notebook = notebook_service.get_notebook(notebook_id)
    if notebook is None:
        await ws.close(code=4004, reason="Notebook not found")
        return None

    # Check if the image needs building
    tag = container_service.get_image_tag(
        notebook.python_version, notebook.gpu
    )
    if not container_service.has_image(tag):
        await send_json(ws, {
            "type": "container_state",
            "status": "building",
            "message": f"Building image {tag}...",
        })
        try:
            await _stream_image_build(ws, notebook, tag)
        except Exception as e:
            await send_json(ws, {
                "type": "container_state",
                "status": "error",
                "message": f"Image build failed: {e}",
            })
            await ws.close(code=4010, reason="Image build failed")
            return None

    # Start container (image now exists)
    state = container_service.start_container(
        notebook_id,
        python_version=notebook.python_version,
        gpu=notebook.gpu,
    )
    await send_json(ws, {
        "type": "container_state",
        "status": state.status.value,
        "message": state.error_message,
    })

    if state.status != ContainerStatus.READY:
        await ws.close(code=4010, reason="Container failed to start")
        return None

    if settings.docker_network:
        exec_host = container_service.get_container_name(notebook_id)
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
        return None

    return executor


@app.websocket("/ws/notebooks/{notebook_id}")
async def notebook_websocket(
    ws: WebSocket, notebook_id: str
) -> None:
    await ws.accept()
    print(f"[WS] accepted for {notebook_id[:8]}", flush=True)

    # Disconnect old executor and cancel stale tasks from prior connection
    old_executor = active_executors.pop(notebook_id, None)
    if old_executor:
        await old_executor.disconnect()
        print("[WS] disconnected old executor", flush=True)
    _cancel_stale_tasks(notebook_id)

    executor = await _setup_connection(ws, notebook_id)
    if executor is None:
        print("[WS] setup_connection failed", flush=True)
        return

    print(f"[WS] executor connected on port {executor.port}", flush=True)

    active_executors[notebook_id] = executor
    execution_locks[notebook_id] = asyncio.Lock()
    execution_counts.setdefault(notebook_id, 0)
    _pending_tasks[notebook_id] = set()

    try:
        await _message_loop(ws, notebook_id, executor)
    except WebSocketDisconnect:
        print(f"[WS] client disconnected {notebook_id[:8]}", flush=True)
    except Exception:
        logger.exception("WebSocket error for notebook %s", notebook_id)
    finally:
        await executor.disconnect()
        if active_executors.get(notebook_id) is executor:
            active_executors.pop(notebook_id, None)
            _cancel_stale_tasks(notebook_id)


async def _message_loop(
    ws: WebSocket, notebook_id: str, executor: ExecutorClient
) -> None:
    while True:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "execute":
            print(f"[WS] execute received: {msg.get('cell_id', '')[:8]}", flush=True)
            container_service.record_activity(notebook_id)
            task = asyncio.create_task(
                _handle_execute(
                    ws, notebook_id,
                    msg.get("cell_id", ""),
                    msg.get("code", ""),
                    executor,
                )
            )
            tasks = _pending_tasks.get(notebook_id)
            if tasks is not None:
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        elif msg_type == "interrupt":
            try:
                await executor.interrupt()
            except Exception:
                logger.warning(
                    "Failed to send interrupt for %s",
                    msg.get("cell_id", ""),
                )


async def _process_executor_msg(
    ws: WebSocket, cell_id: str, msg: dict,
    outputs: list[Output],
) -> str | None:
    """Process one executor message. Returns final state if terminal."""
    msg_type = msg.get("type")

    if msg_type == "output":
        text = msg.get("text", "")
        if text.strip():
            print(f"[WS] relay: {text[:60].strip()}", flush=True)
        await send_json(ws, {
            "type": "output",
            "cell_id": cell_id,
            "stream": msg.get("stream", "stdout"),
            "text": text,
        })
        outputs.append(Output(
            output_type=OutputType(msg.get("stream", "stdout")),
            content=msg.get("text", ""),
        ))
    elif msg_type == "result":
        await send_json(ws, {
            "type": "result",
            "cell_id": cell_id,
            "data": msg.get("data", ""),
        })
        outputs.append(Output(
            output_type=OutputType.RESULT,
            content=msg.get("data", ""),
        ))
    elif msg_type == "display":
        await send_json(ws, {
            "type": "display",
            "cell_id": cell_id,
            "display_type": msg.get("display_type", ""),
            "mime": msg.get("mime", ""),
            "data": msg.get("data", ""),
            "filename": msg.get("filename", ""),
        })
    elif msg_type == "error":
        await send_json(ws, {
            "type": "error",
            "cell_id": cell_id,
            "ename": msg.get("ename", ""),
            "evalue": msg.get("evalue", ""),
            "traceback": msg.get("traceback", []),
        })
        outputs.append(Output(
            output_type=OutputType.ERROR,
            content=(
                f"{msg.get('ename', '')}: {msg.get('evalue', '')}"
            ),
        ))
        return "errored"
    elif msg_type == "state":
        es = msg.get("execution_state", "")
        if es in ("completed", "errored"):
            return es

    return None


async def _handle_execute(
    ws: WebSocket,
    notebook_id: str,
    cell_id: str,
    code: str,
    executor: ExecutorClient,
) -> None:
    lock = execution_locks[notebook_id]
    print(f"[WS] _handle_execute waiting for lock {cell_id[:8]}", flush=True)

    async with lock:
        print(f"[WS] lock acquired, executing {cell_id[:8]}", flush=True)
        execution_counts[notebook_id] += 1
        exec_count = execution_counts[notebook_id]

        await send_json(ws, {
            "type": "state",
            "cell_id": cell_id,
            "execution_state": "running",
            "execution_count": exec_count,
        })

        notebook = notebook_service.get_notebook(notebook_id)
        _set_cell_running(notebook, cell_id, exec_count)

        final_state, outputs = await _stream_execution(
            ws, cell_id, code, executor
        )
        print(f"[WS] execution done: {final_state} {cell_id[:8]}", flush=True)

        await send_json(ws, {
            "type": "state",
            "cell_id": cell_id,
            "execution_state": final_state,
            "execution_count": exec_count,
        })

        _persist_cell_outputs(
            notebook, notebook_id, cell_id,
            final_state, exec_count, outputs,
        )


async def _stream_execution(
    ws: WebSocket, cell_id: str, code: str,
    executor: ExecutorClient,
) -> tuple[str, list[Output]]:
    final_state = "completed"
    outputs: list[Output] = []

    try:
        async for msg in executor.execute(code):
            terminal = await _process_executor_msg(
                ws, cell_id, msg, outputs
            )
            if terminal:
                final_state = terminal
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Execution error for cell %s", cell_id)
        final_state = "errored"
        await send_json(ws, {
            "type": "error",
            "cell_id": cell_id,
            "ename": "ExecutionError",
            "evalue": str(e),
            "traceback": [],
        })

    return final_state, outputs


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
    notebook_service.save_notebook(notebook)
