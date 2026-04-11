# Jupyter Redux

Jupyter notebooks, reimagined with containers. Every notebook runs in its own isolated Docker container instead of a shared kernel. Install packages per-notebook, get full GPU passthrough, and never worry about dependency conflicts again.

## Prerequisites

- Docker (with Docker Compose)
- NVIDIA Container Toolkit (optional, for GPU support)

## Quick Start

```bash
# Clone the repo
git clone https://github.com/stevemurr/jupyter-redux.git
cd jupyter-redux

# Start the application
docker compose up --build

# Open in your browser
# http://localhost:8000
```

To disable GPU support (e.g. on a machine without an NVIDIA GPU):

```bash
JREDUX_GPU_ENABLED=false docker compose up --build
```

## How It Works

Each notebook you create gets its own Docker container. There are no shared kernels -- your environment is yours alone.

1. **Create a notebook** from the home screen
2. **Install packages** in a setup cell at the top: `pip install pandas torch`
3. **Write and run code** in cells below -- packages persist across sessions
4. **GPU access** is automatic on hosts with NVIDIA GPUs and the Container Toolkit

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server locally (requires Docker running)
uvicorn src.app:app --reload --port 8000

# Run tests and lint
cd src && pytest && ruff check .
```

## Architecture

```
Browser <--HTTP/WS--> FastAPI Server <--Docker API--> Container per Notebook
                          |                                |
                      Notebook CRUD                   Code execution
                      Cell management                 Package installation
                      Static files                    GPU passthrough
```

The server manages notebook metadata and proxies code execution to per-notebook containers over an internal network. Containers run a lightweight Python executor that accepts code over HTTP and streams output back via WebSocket.

## License

MIT
