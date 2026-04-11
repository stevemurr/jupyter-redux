# Quickstart: Jupyter Redux

## Prerequisites

- Python 3.11+
- Docker Engine 24+ with Docker daemon running
- NVIDIA Container Toolkit (for GPU support)
- NVIDIA GPU drivers installed on host

## Setup

```bash
# Clone and enter the project
cd jupyter-redux

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Build the base container image for notebook environments
docker build -t jupyter-redux-base:latest -f src/executor/Dockerfile .

# Create data directory for notebook storage
mkdir -p data/notebooks
```

## Run

```bash
# Start the application
python -m uvicorn src.app:app --host 0.0.0.0 --port 8000

# Or with auto-reload for development
python -m uvicorn src.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser.

## First Notebook

1. Click **New Notebook** on the landing page
2. Wait for the container to provision (first time may take a few
   seconds while the base image initializes)
3. Type `print("Hello from Jupyter Redux!")` in the first cell
4. Press **Shift+Enter** to execute
5. See the output appear below the cell

## Install a Package

1. Add a new cell at the top of the notebook (press **A** in
   command mode)
2. Type `pip install numpy`
3. Press **Shift+Enter** — the package installs in this notebook's
   container
4. In the next cell, type `import numpy; print(numpy.__version__)`
5. Press **Shift+Enter** to verify

## GPU Verification

```python
# In a notebook cell:
import torch
print(f"CUDA available: {torch.cuda.is_available()}")
print(f"GPU count: {torch.cuda.device_count()}")
print(f"GPU name: {torch.cuda.get_device_name(0)}")
```

## Run Tests

```bash
# Unit tests
pytest tests/unit/ -v

# Integration tests (requires Docker running)
pytest tests/integration/ -v

# All tests with coverage
pytest --cov=src --cov-fail-under=80 -v

# E2E tests (requires application running)
pytest tests/e2e/ -v
```

## Project Structure

```
src/
├── app.py            # FastAPI entry point
├── container.py      # Docker container management
├── notebook.py       # Notebook CRUD and persistence
├── executor.py       # Code execution client (host side)
├── models.py         # Pydantic data models
├── websocket.py      # WebSocket handlers
├── config.py         # Configuration
├── static/           # Frontend (HTML/CSS/JS)
└── executor/         # Execution server (runs inside containers)
    ├── server.py
    └── Dockerfile
data/
└── notebooks/        # Notebook JSON files
tests/
├── unit/
├── integration/
└── e2e/
```

## Key Concepts

- **Each notebook = one container**: Notebooks are fully isolated.
  Installing a package in one notebook does not affect others.
- **Environment persistence**: Installed packages persist across
  sessions via Docker named volumes. No need to re-run setup cells
  after reopening a notebook.
- **GPU passthrough**: All host GPUs are available inside every
  notebook container via NVIDIA Container Toolkit.
- **Keyboard shortcuts**: All Jupyter keyboard shortcuts work
  identically — Shift+Enter, Ctrl+Enter, Esc for command mode,
  Enter for edit mode, A/B to add cells, DD to delete, etc.
