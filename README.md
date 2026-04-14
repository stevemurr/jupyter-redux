# Jupyter Redux

Jupyter notebooks, reimagined around **Environments**. Each environment is its own Docker container with its own Python version, dependencies, and optional GPU. Notebooks live inside an environment and share its container, so installing a package once makes it available to every notebook in that environment — without polluting any other.

No shared kernel. No global pip install. No dependency roulette.

## Prerequisites

- Docker (with Docker Compose)
- NVIDIA Container Toolkit (optional, for GPU support)

## Quick Start

```bash
git clone https://github.com/stevemurr/jupyter-redux.git
cd jupyter-redux

# One-time: create the host directories that environments bind-mount
./scripts/init-shared.sh

# Start the app (builds the executor base images on first run)
docker compose up --build

# Open http://localhost:8000
```

To run on a machine without an NVIDIA GPU:

```bash
JREDUX_GPU_ENABLED=false docker compose up --build
```

## Core Concepts

**Environment** — a named Docker container with a chosen Python version and GPU flag. The container boundary.

**Notebook** — a document (cells, outputs, metadata) that belongs to one environment. Notebooks in the same environment share the same container, filesystem, and installed packages.

**Shared mounts** — two host directories are bind-mounted into every environment:

- `/shared/datasets` — read-only. Drop files on the host, see them in every container immediately.
- `/shared/artifacts` — read-write. Files written here are owned by your host user, so they survive container restarts and are easy to pick up outside the app.

## Features

- **CodeMirror 6 editor** with vim-style command/edit modes — `j`/`k` to navigate, `a`/`b` to insert cells, `dd` to delete, `z` to undo delete, `Shift+Enter`/`Ctrl+Enter`/`Alt+Enter` to execute.
- **Auto-save** with per-cell debounce; edits flush on page unload via `fetch keepalive`.
- **Live training Monitor** — `mon = Monitor(title="Training", total_steps=N); mon.log(step=i, loss=..., accuracy=...)` renders live charts in the sidebar as your loop runs.
- **`%apt install`** cell magic — install system packages into the running container without rebuilding the image.
- **`pip install` detection** — routed through `uv pip install` when available for fast installs.
- **Host & container metrics** — CPU, memory, and GPU utilization streamed to the sidebar over WebSocket.
- **Build log streaming** — watch `docker build` output live the first time an environment comes up.
- **Persistent execution across reconnects** — WebSocket disconnects don't kill in-flight cells; output is buffered and replayed on reconnect.
- **Repository cloning** — clone a git repo directly into an environment and optionally `uv sync` it.

## Architecture

```
Browser <--HTTP/WS--> FastAPI server <--Docker API--> Environment container (1..N)
                          |                                    |
                   Environment/notebook CRUD            Cell execution
                   Static assets                        Package installation
                   Metrics aggregation                  GPU passthrough
                                                        /shared mounts
```

The FastAPI server owns environment and notebook metadata (persisted as JSON under `./data/`) and proxies code execution to per-environment containers over an internal Docker network. Each container runs a lightweight Python executor that accepts code over HTTP and streams stdout, stderr, rich display output, and training metrics back over WebSocket.

## Configuration

Environment variables (set in `.env` or your shell):

| Variable | Default | Purpose |
| --- | --- | --- |
| `JREDUX_GPU_ENABLED` | `true` | Enable NVIDIA GPU passthrough for new environments |
| `JREDUX_HOST_UID` / `JREDUX_HOST_GID` | `1000` / `1000` | Host uid/gid that env containers drop privileges to, so files in `/shared/artifacts` are owned by you |
| `JREDUX_DATASETS_PATH` | `$HOME/jupyter-redux/datasets` | Host path mounted read-only at `/shared/datasets` |
| `JREDUX_ARTIFACTS_PATH` | `$HOME/jupyter-redux/artifacts` | Host path mounted read-write at `/shared/artifacts` |
| `JREDUX_HF_TOKEN` | — | HuggingFace token injected into environment containers |
| `JREDUX_DOCKER_NETWORK` | `jredux` | Docker network name for the server and env containers |

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server locally (Docker must be running on the host)
uvicorn src.app:app --reload --port 8000

# Tests and lint
cd src && pytest && ruff check .
```

The frontend is vanilla JavaScript + CodeMirror 6. CodeMirror is bundled once by `esbuild` (see `cm-entry.js` and `package.json`); there is no framework and no dev server.

## License

MIT
