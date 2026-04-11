# Research: Container-Based Notebook Environments

**Date**: 2026-04-10
**Feature**: [spec.md](spec.md)

## R1: Code Execution Inside Docker Containers

**Decision**: Custom lightweight TCP execution server running inside
each container, rather than Jupyter's kernel protocol (ZeroMQ/IPython).

**Rationale**: Jupyter's kernel protocol (ipykernel + ZeroMQ) is
powerful but heavyweight — it requires IPython, ZeroMQ bindings, and
the full Jupyter messaging spec. Since we control both sides of the
protocol, a simple TCP server that receives Python code as text,
executes it via `exec()` with captured stdout/stderr, and streams
output back line-by-line is sufficient and dramatically simpler.

**Alternatives considered**:
- **Jupyter kernel protocol**: Full-featured but adds ~50MB to each
  container image and requires implementing the full messaging spec
  on the host side. Overkill for v1.
- **HTTP-based execution**: Simpler protocol but HTTP request/response
  doesn't naturally support streaming output. Would require SSE or
  polling, adding complexity.
- **Docker exec**: Using `docker exec` for each cell. Simple but no
  persistent Python process state (variables lost between cells).

**Implementation pattern**:
The executor server (`src/executor/server.py`) runs as PID 1 in the
container. It listens on a TCP port (default 9999), maintains a
persistent Python namespace (globals dict), and for each code
submission:
1. Redirects stdout/stderr to capture buffers
2. Calls `exec(code, namespace)` or `eval(code, namespace)`
3. Streams captured output back over the socket in real-time
4. Returns final result or exception info

## R2: Real-Time Communication Protocol

**Decision**: WebSocket for bidirectional real-time communication
between browser and backend.

**Rationale**: Cell execution requires streaming output (stdout lines
arriving over time) and supports interruption (client sends interrupt
signal mid-execution). WebSocket provides full-duplex communication
that handles both naturally.

**Alternatives considered**:
- **Server-Sent Events (SSE)**: Server-to-client only. Would require
  a separate REST endpoint for client-to-server actions (execute,
  interrupt), complicating the protocol.
- **Long polling**: Higher latency, more HTTP overhead, harder to
  implement streaming.

**Message protocol** (JSON over WebSocket):
```
Client → Server:
  {"type": "execute", "cell_id": "<uuid>", "code": "<source>"}
  {"type": "interrupt", "cell_id": "<uuid>"}

Server → Client:
  {"type": "output", "cell_id": "<uuid>", "stream": "stdout|stderr", "text": "..."}
  {"type": "result", "cell_id": "<uuid>", "data": "..."}
  {"type": "error", "cell_id": "<uuid>", "ename": "...", "evalue": "...", "traceback": [...]}
  {"type": "state", "cell_id": "<uuid>", "state": "running|completed|errored"}
  {"type": "container_state", "state": "starting|ready|stopped|error", "message": "..."}
```

## R3: Docker Container Lifecycle with GPU Passthrough

**Decision**: Docker SDK for Python (`docker` package) with NVIDIA
Container Toolkit for GPU access via `device_requests`.

**Rationale**: The Docker SDK provides a native Python interface for
all container operations. NVIDIA Container Toolkit integrates with
Docker's `--gpus` flag, exposed in the SDK as `device_requests`.

**Alternatives considered**:
- **Subprocess calls to docker CLI**: Works but fragile (parsing
  stdout), no structured error handling, harder to test.
- **Podman**: Compatible API but less mature GPU support and smaller
  ecosystem.

**Key patterns**:
```python
import docker

client = docker.from_env()

# Create container with GPU access
container = client.containers.run(
    image="jupyter-redux-base:latest",
    detach=True,
    device_requests=[
        docker.types.DeviceRequest(
            count=-1,  # all GPUs
            capabilities=[["gpu"]]
        )
    ],
    volumes={
        f"jredux-{notebook_id}": {"bind": "/env", "mode": "rw"}
    },
    ports={"9999/tcp": None},  # auto-assign host port
    environment={"PYTHONPATH": "/env/lib"},
)
```

**Container lifecycle**:
1. **Create**: On notebook creation → build from base image, attach
   named volume, start executor server
2. **Start**: On notebook open → start stopped container (fast, <2s)
3. **Stop**: On idle timeout → stop container (preserves filesystem)
4. **Destroy**: On notebook delete → remove container + volume

## R4: Environment Persistence Strategy

**Decision**: Docker named volumes mounted at a known path inside
each container. Packages installed via pip persist on the volume.

**Rationale**: Named volumes survive container stop/start/remove
cycles. By mounting the volume at the Python site-packages path
(or a custom PYTHONPATH directory), all pip-installed packages
persist automatically without committing container layers.

**Alternatives considered**:
- **Docker commit after installs**: Creates a new image layer after
  each `pip install`. Consumes more disk, slower, complex image
  management.
- **Bind mounts to host directory**: Works but volume management
  is more ergonomic and Docker-native.
- **Custom package cache**: Requires custom pip configuration inside
  the container. Adds complexity.

**Volume strategy**:
- Volume name: `jredux-{notebook_id}`
- Mount point: `/env` inside container
- Pip configured via `PIP_TARGET=/env/lib`
- `PYTHONPATH=/env/lib` in container environment
- Base image includes Python 3.11+ and pip only

## R5: Notebook Storage Format

**Decision**: JSON files on the host filesystem, one file per
notebook, stored in `data/notebooks/`.

**Rationale**: JSON is human-readable, diff-friendly, and aligns
with Jupyter's `.ipynb` format. Filesystem storage avoids a database
dependency for v1. Each notebook file contains all cells, outputs,
and metadata.

**Alternatives considered**:
- **SQLite**: Structured queries but overkill for single-user with
  <100 notebooks. Adds a dependency.
- **Jupyter .ipynb format**: Compatible but includes nbformat
  versioning complexity we don't need. Our format is simpler.

**File naming**: `{notebook_id}.json` in `data/notebooks/`.

## R6: Frontend Approach

**Decision**: Vanilla HTML/CSS/JS with CodeMirror 6 for code editing.
No frontend framework.

**Rationale**: The user specified HTML/CSS/JS. The notebook UI has
a limited number of component types (cell list, cell editor, output
display, toolbar). Vanilla JS keeps the bundle small (~170KB gzipped
total with CodeMirror) and avoids framework lock-in. CodeMirror 6
provides syntax highlighting, line numbers, and keybinding support
essential for a code editor.

**Alternatives considered**:
- **React/Vue/Svelte**: Powerful but adds bundle size, build tooling,
  and complexity beyond what's needed for this UI surface area.
- **Monaco Editor**: Feature-rich but ~2MB gzipped. Exceeds bundle
  budget.
- **Ace Editor**: Viable but CodeMirror 6 is more modern, modular,
  and lighter.

## R7: Keyboard Shortcut Implementation

**Decision**: Implement Jupyter-compatible keyboard shortcuts via a
custom keybinding layer that intercepts keyboard events in command
mode and edit mode.

**Rationale**: FR-014 requires exact Jupyter shortcut parity. The
notebook operates in two modes (like Jupyter):
- **Command mode** (Esc): Cell-level operations (add, delete, move,
  change type, run)
- **Edit mode** (Enter): Text editing within a cell (handled by
  CodeMirror)

**Key bindings** (Jupyter defaults):
| Shortcut | Mode | Action |
|----------|------|--------|
| Shift+Enter | Both | Execute cell, move to next |
| Ctrl+Enter | Both | Execute cell, stay |
| Alt+Enter | Both | Execute cell, insert below |
| Esc | Edit | Enter command mode |
| Enter | Command | Enter edit mode |
| A | Command | Insert cell above |
| B | Command | Insert cell below |
| DD | Command | Delete cell |
| M | Command | Change to markdown |
| Y | Command | Change to code |
| Up/K | Command | Select previous cell |
| Down/J | Command | Select next cell |
| Ctrl+Shift+- | Edit | Split cell at cursor |
| Z | Command | Undo cell delete |
| Shift+M | Command | Merge cells |
