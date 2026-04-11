# Implementation Plan: Container-Based Notebook Environments

**Branch**: `001-container-notebook-envs` | **Date**: 2026-04-10 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-container-notebook-envs/spec.md`

## Summary

Build a browser-based notebook application where each notebook runs
inside its own Docker container instead of a shared kernel. A Python
backend (FastAPI) manages container lifecycle and proxies code
execution over WebSockets. The frontend is vanilla HTML/CSS/JS with
CodeMirror for code editing. Containers get full GPU access via
NVIDIA Container Toolkit and persist installed packages through
Docker named volumes.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: FastAPI, uvicorn, docker (Python SDK),
websockets, Pydantic
**Storage**: Filesystem (JSON files for notebooks), Docker named
volumes (for container environment state)
**Testing**: pytest, pytest-asyncio, httpx (async test client),
Playwright (E2E)
**Target Platform**: Linux server with Docker and NVIDIA Container
Toolkit
**Project Type**: Web application (backend serves frontend)
**Performance Goals**: 3s TTI, 100ms cell dispatch, 60fps scroll,
30s notebook creation including container provisioning
**Constraints**: 512MB client heap, 500KB gzipped JS bundle, 10
concurrent active notebooks per host
**Scale/Scope**: Single-user/small-team, on-premises, ~5 screens
(notebook list, notebook editor, settings)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### I. Code Quality

| Rule | Status | Plan |
|------|--------|------|
| Readability / self-documenting | PASS | Clear module names: `container.py`, `notebook.py`, `executor.py` |
| Single responsibility | PASS | One module per concern; no god classes |
| Cyclomatic complexity <= 10 | PASS | Enforce via ruff `C901` rule |
| Consistent style | PASS | ruff + black in CI; pre-commit hooks |
| No dead code | PASS | ruff `F401`/`F841` rules |
| Type annotations on public APIs | PASS | Pydantic models + explicit return types on all route handlers and service methods |

### II. Testing Standards

| Rule | Status | Plan |
|------|--------|------|
| 80% line coverage minimum | PASS | pytest-cov with `--cov-fail-under=80` |
| 100% branch coverage on critical paths | PASS | Container lifecycle, notebook CRUD, WebSocket execution |
| Balanced test pyramid | PASS | Unit (services, models), integration (Docker, WebSocket), E2E (Playwright) |
| Test independence | PASS | Each test creates/destroys its own fixtures |
| Regression tests for bugs | PASS | Enforced in PR checklist |
| Descriptive test names | PASS | Convention: `test_<unit>_<scenario>_<expected>` |

### III. User Experience Consistency

| Rule | Status | Plan |
|------|--------|------|
| Follow established patterns | PASS | Jupyter-compatible cell model and shortcuts |
| 200ms feedback | PASS | Optimistic UI updates; WebSocket push for execution state |
| Actionable error messages | PASS | Error handler maps Docker/system errors to user-facing messages |
| Keyboard accessibility | PASS | Full keyboard navigation; ARIA labels on all controls |
| State persistence | PASS | Auto-save to JSON on every cell change; reconnect on connection drop |
| Responsive 1024-2560px | PASS | CSS flexbox layout with min-width constraints |

### IV. Performance Requirements

| Rule | Status | Plan |
|------|--------|------|
| TTI <= 3s | PASS | Vanilla JS, no framework bundle; CodeMirror loaded async |
| Cell operations <= 100ms | PASS | WebSocket dispatch; DOM updates via direct manipulation |
| 500 cells render <= 1s | PASS | Virtual scrolling for large notebooks |
| Heap <= 512MB | PASS | Cell output truncation; lazy rendering of off-screen cells |
| Bundle <= 500KB gzipped | PASS | Vanilla JS (~20KB) + CodeMirror (~150KB gzipped) |
| Perf benchmarks in CI | PASS | Lighthouse CI for TTI/bundle; custom benchmark for cell ops |

**Gate Result**: ALL PASS — proceed to Phase 0.

## Project Structure

### Documentation (this feature)

```text
specs/001-container-notebook-envs/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── rest-api.md
│   └── websocket.md
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/
├── app.py               # FastAPI entry point, app factory, middleware
├── container.py          # Docker container lifecycle management
├── notebook.py           # Notebook CRUD, JSON persistence
├── executor.py           # Code execution protocol (host-side client)
├── models.py             # Pydantic data models (Notebook, Cell, Output)
├── websocket.py          # WebSocket handlers for cell execution
├── config.py             # Application configuration
├── static/               # Frontend served by FastAPI
│   ├── index.html        # Notebook list page
│   ├── notebook.html     # Notebook editor page
│   ├── css/
│   │   └── style.css     # Application styles
│   └── js/
│       ├── app.js        # Shared utilities, routing
│       ├── notebook.js   # Notebook editor logic
│       ├── cell.js       # Cell component (render, edit, execute)
│       └── ws.js         # WebSocket client wrapper
├── executor/             # Runs INSIDE each container
│   ├── server.py         # TCP execution server (receives code, streams output)
│   └── Dockerfile        # Base image for notebook containers
docker/
│   └── base.Dockerfile   # Alternative location if executor/ feels cluttered
data/
└── notebooks/            # Stored notebook JSON files

tests/
├── conftest.py           # Shared fixtures (Docker client, test notebooks)
├── unit/
│   ├── test_notebook.py  # Notebook CRUD logic
│   ├── test_models.py    # Pydantic model validation
│   └── test_executor.py  # Execution protocol parsing
├── integration/
│   ├── test_container.py # Docker container lifecycle
│   ├── test_websocket.py # WebSocket execution flow
│   └── test_gpu.py       # GPU passthrough verification
└── e2e/
    └── test_notebook_flow.py  # Full user journey via Playwright
```

**Structure Decision**: Single-project layout. The Python backend
serves the static frontend directly via FastAPI's `StaticFiles`
mount. No separate build step for the frontend. The executor code
lives inside `src/executor/` with its own Dockerfile since it runs
inside notebook containers, not on the host.

## Complexity Tracking

> No constitution violations to justify.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
