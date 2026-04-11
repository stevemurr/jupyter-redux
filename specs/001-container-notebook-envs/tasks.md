# Tasks: Container-Based Notebook Environments

**Input**: Design documents from `/specs/001-container-notebook-envs/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Not explicitly requested in the feature specification. Test infrastructure is included in setup; dedicated test tasks can be added via `/speckit-tasks` with a TDD flag.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Project initialization and directory structure

- [x] T001 Create project directory structure per implementation plan: `src/`, `src/static/css/`, `src/static/js/`, `src/executor/`, `data/notebooks/`, `tests/unit/`, `tests/integration/`, `tests/e2e/`
- [x] T002 Initialize Python project with `pyproject.toml` (project metadata, ruff + black config, pytest config with `--cov-fail-under=80`) and `requirements.txt` (fastapi, uvicorn[standard], docker, pydantic, websockets, httpx, pytest, pytest-asyncio, pytest-cov)
- [x] T003 [P] Configure linting and formatting: add ruff rules (C901 max-complexity=10, F401, F841, I001) and black config to `pyproject.toml`
- [x] T004 [P] Create `.gitignore` for Python (`__pycache__/`, `.venv/`, `*.pyc`), Docker, IDE files, and `data/notebooks/*.json`

**Checkpoint**: Project skeleton ready — dependency install and `ruff check` should pass.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [x] T005 Implement Pydantic data models in `src/models.py`: Notebook, Cell, Output, CellType enum (code/markdown), OutputType enum (stdout/stderr/result/error), ExecutionState enum (idle/running/completed/errored), ContainerStatus enum, ContainerState — with all validation rules from data-model.md
- [x] T006 [P] Implement application configuration in `src/config.py`: host, port, data directory path, Docker base image name, executor port (9999), container idle timeout, using Pydantic BaseSettings with env var overrides
- [x] T007 [P] Create base Dockerfile for notebook containers in `src/executor/Dockerfile`: Python 3.11-slim base, install pip, set `PIP_TARGET=/env/lib` and `PYTHONPATH=/env/lib`, copy `server.py`, expose port 9999, CMD to run server.py
- [x] T008 Implement TCP execution server in `src/executor/server.py`: listen on port 9999, maintain persistent Python namespace (globals dict), accept JSON-framed messages, `exec()`/`eval()` submitted code, redirect and stream stdout/stderr back line-by-line, return result or exception info with traceback, handle SIGINT for interruption
- [x] T009 Implement FastAPI application factory in `src/app.py`: create FastAPI app, mount `src/static` as StaticFiles at `/`, configure CORS middleware, add global exception handler that maps errors to actionable user-facing messages, include health check endpoint at `GET /api/health`
- [x] T010 [P] Create shared test fixtures in `tests/conftest.py`: async test client fixture (httpx.AsyncClient), temporary notebook data directory, sample Notebook/Cell model factories, Docker client mock fixture

**Checkpoint**: Foundation ready — `python -c "from src.models import Notebook"` works, Dockerfile builds, executor server starts standalone.

---

## Phase 3: User Story 1 - Create and Execute a Notebook in an Isolated Container (Priority: P1) MVP

**Goal**: User opens browser, creates a notebook, writes code in a cell, executes it, and sees output — all running inside an isolated Docker container.

**Independent Test**: Create a notebook, type `print("hello")`, press Shift+Enter, verify `hello` appears as output below the cell.

### Implementation for User Story 1

- [x] T011 [US1] Implement Docker container lifecycle manager in `src/container.py`: create_container() with named volume mount (jredux-{id} at /env), start_container(), stop_container(), destroy_container() with volume removal, get_container_status() returning ContainerState, auto-assign host port for executor TCP connection
- [x] T012 [US1] Implement notebook storage service in `src/notebook.py`: create_notebook() generating UUID and writing JSON to `data/notebooks/{id}.json`, get_notebook() reading and parsing JSON, save_notebook() writing updated JSON, update index.json on create/save, add_cell() appending Cell with UUID to notebook, update_cell() modifying cell source/type
- [x] T013 [US1] Implement execution protocol client (host side) in `src/executor.py`: connect to container's executor TCP server via mapped host port, send code as JSON message, receive streamed output lines, parse result/error responses, handle connection failures with retries, handle interrupt by closing connection
- [x] T014 [US1] Implement notebook REST API routes in `src/app.py`: `POST /api/notebooks` (create notebook + provision container), `GET /api/notebooks/{id}` (return notebook with cells and container state)
- [x] T015 [US1] Implement cell REST API routes in `src/app.py`: `POST /api/notebooks/{id}/cells` (add cell at position with type and source), `PUT /api/notebooks/{id}/cells/{cell_id}` (update cell source or type)
- [x] T016 [US1] Implement container management REST routes in `src/app.py`: `POST /api/notebooks/{id}/container/start`, `POST /api/notebooks/{id}/container/stop`, `GET /api/notebooks/{id}/container/status`
- [x] T017 [US1] Implement WebSocket endpoint in `src/websocket.py`: accept connection at `ws://host/ws/notebooks/{id}`, verify notebook exists (close 4004 if not), ensure container is running and send container_state, handle "execute" messages by forwarding code to executor and streaming output/state/result/error back, handle "interrupt" messages by sending SIGINT, queue concurrent executions sequentially
- [x] T018 [P] [US1] Build notebook list page in `src/static/index.html`: page layout with header, "New Notebook" button that calls POST /api/notebooks and navigates to editor, placeholder notebook card list (full list in US4)
- [x] T019 [P] [US1] Build base application styles in `src/static/css/style.css`: CSS reset, flexbox page layout (1024-2560px), notebook toolbar styles, cell container styles (selected/unselected border), code editor area, output area (stdout green-ish, stderr red-ish, error with traceback formatting), execution state indicators (running spinner, completed check, error icon), command-mode vs edit-mode cell border colors
- [x] T020 [US1] Implement WebSocket client wrapper in `src/static/js/ws.js`: connect to `ws://host/ws/notebooks/{id}`, parse incoming JSON messages, dispatch to registered callbacks by message type, send execute/interrupt messages, auto-reconnect on disconnect with exponential backoff, emit container_state events
- [x] T021 [US1] Implement cell component in `src/static/js/cell.js`: create cell DOM element with CodeMirror 6 editor (Python syntax highlighting, line numbers), output display area below editor, execution count badge (`In [N]:`), execution state indicator, cell type toggle (code/markdown), cell toolbar (run button, delete button), focus/blur handling for command/edit mode transitions
- [x] T022 [US1] Build notebook editor page with keyboard shortcuts in `src/static/notebook.html` and `src/static/js/notebook.js`: load notebook via GET API, render ordered cell list, command mode and edit mode state machine, Jupyter keyboard shortcuts (Shift+Enter execute+advance, Ctrl+Enter execute-in-place, Alt+Enter execute+insert-below, Esc enter command mode, Enter enter edit mode, A insert above, B insert below, DD delete, Y change to code, M change to markdown, Up/K select previous, Down/J select next, Z undo delete), wire cell execution: shortcut → ws.send(execute) → display streamed output → update execution count and state

**Checkpoint**: User Story 1 fully functional — create notebook, execute code cells, see streamed output. Notebooks are isolated in separate containers.

---

## Phase 4: User Story 2 - Install Packages via Environment Setup Cells (Priority: P2)

**Goal**: User adds a cell with `pip install <package>`, executes it, and the package installs in the notebook's container. Packages persist across sessions.

**Independent Test**: Execute `pip install requests` in a cell, then `import requests` in the next cell — verify it succeeds. Close and reopen the notebook — verify `requests` is still importable.

### Implementation for User Story 2

- [x] T023 [US2] Add pip/shell command detection and execution in `src/executor/server.py`: detect lines starting with `pip install`, `pip uninstall`, or `!` prefix (shell escape), route to `subprocess.run()` instead of `exec()`, stream subprocess stdout/stderr back in real-time, return exit code as result
- [x] T024 [US2] Ensure Docker volume persistence for packages in `src/container.py`: verify named volume `jredux-{id}` is created with `PIP_TARGET=/env/lib` and `PYTHONPATH=/env/lib` environment variables, verify volume survives container stop/start cycles, add integration note in container create that volume mount is at `/env`
- [x] T025 [P] [US2] Add setup cell visual treatment in `src/static/js/cell.js`: detect `pip install` prefix in cell source, render distinct styling (package icon, installation-in-progress animation), show installation success/failure badge after execution
- [x] T026 [US2] Add shared utility functions in `src/static/js/app.js`: API fetch helpers (GET, POST, PUT, DELETE with JSON parsing and error handling), notebook URL routing (extract notebook ID from URL), common DOM utilities

**Checkpoint**: Users can install packages in a notebook's container and packages persist across sessions. Setup cells have distinct visual treatment.

---

## Phase 5: User Story 3 - GPU-Accelerated Computing (Priority: P3)

**Goal**: Notebook containers have full GPU access. Users can run CUDA/PyTorch/TensorFlow workloads.

**Independent Test**: Execute `import torch; print(torch.cuda.is_available())` in a notebook cell on a GPU-equipped host — verify output is `True`.

### Implementation for User Story 3

- [x] T027 [US3] Add NVIDIA GPU passthrough to container creation in `src/container.py`: add `device_requests=[DeviceRequest(count=-1, capabilities=[["gpu"]])]` to `client.containers.run()`, make GPU attachment configurable (enabled by default, disable via config for non-GPU hosts), handle graceful fallback when NVIDIA runtime is not available (log warning, create container without GPU)
- [x] T028 [US3] Update base Dockerfile for GPU compatibility in `src/executor/Dockerfile`: use `nvidia/cuda:12.x-runtime-ubuntu22.04` as optional GPU base image (multi-stage: GPU and non-GPU variants), ensure Python 3.11 and pip installed on GPU image, document NVIDIA Container Toolkit prerequisite
- [x] T029 [P] [US3] Add GPU status indicator to notebook UI in `src/static/js/notebook.js`: query container status for GPU availability on notebook open, display GPU badge in notebook toolbar (green checkmark if available, gray if unavailable), show GPU device name on hover/tooltip

**Checkpoint**: GPU workloads run inside notebook containers with full CUDA access. Non-GPU hosts degrade gracefully.

---

## Phase 6: User Story 4 - Notebook Management and Persistence (Priority: P4)

**Goal**: Users can list, rename, delete, and reopen notebooks. All content and environment state persists.

**Independent Test**: Create a notebook, add cells with content, close browser, reopen — verify all cells, outputs, and packages are intact. Rename and delete notebooks from the list view.

### Implementation for User Story 4

- [x] T030 [P] [US4] Implement notebook list REST route in `src/app.py`: `GET /api/notebooks` returning all notebooks from index.json with id, name, created_at, updated_at
- [x] T031 [P] [US4] Implement notebook update REST route in `src/app.py`: `PUT /api/notebooks/{id}` for rename with uniqueness validation, update index.json on rename
- [x] T032 [P] [US4] Implement notebook delete REST route in `src/app.py`: `DELETE /api/notebooks/{id}` that removes JSON file, updates index.json, stops and removes Docker container, removes Docker volume
- [x] T033 [US4] Implement cell delete and reorder REST routes in `src/app.py`: `DELETE /api/notebooks/{id}/cells/{cell_id}`, `POST /api/notebooks/{id}/cells/reorder` with full cell_ids array validation
- [x] T034 [US4] Add auto-save on cell change in `src/static/js/notebook.js`: debounced save (500ms after last keystroke) that sends PUT to update cell source via REST, save indicator in toolbar (saving/saved/error), save on blur and before page unload
- [x] T035 [US4] Build full notebook list UI in `src/static/index.html` and `src/static/js/app.js`: fetch and display all notebooks via GET /api/notebooks, render notebook cards with name and last-modified date, inline rename (click name to edit), delete with confirmation dialog, navigate to notebook editor on card click

**Checkpoint**: Full CRUD lifecycle for notebooks. All state persists across browser sessions and container restarts.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [x] T036 [P] Add cell execution interrupt to frontend in `src/static/js/cell.js` and `src/static/js/notebook.js`: add "Stop" button visible during cell execution, wire to WebSocket interrupt message, add Ctrl+C keyboard shortcut in command mode (interrupt current cell), handle KeyboardInterrupt display in output area
- [x] T037 [P] Implement container idle timeout in `src/container.py`: background task that checks last activity timestamp per container, stop containers after configurable idle period (default 30 minutes), restart on next cell execution, track activity on WebSocket messages
- [x] T038 [P] Add actionable error messages for all failure modes in `src/app.py` and `src/static/js/app.js`: Docker daemon unavailable ("Docker is not running. Start Docker and try again."), container start failure with OOM ("Not enough memory to start notebook. Close other notebooks and try again."), no GPU runtime ("GPU support requires NVIDIA Container Toolkit. Notebook will run without GPU."), disk space exhausted, WebSocket disconnect
- [x] T039 [P] Add ARIA labels and keyboard focus management in `src/static/`: ARIA roles on notebook list (listbox), cells (list, listitem), toolbar buttons (button with aria-label), output areas (log role), focus ring on selected cell, screen reader announcements for execution state changes
- [x] T040 Implement responsive CSS layout in `src/static/css/style.css`: test and adjust layout across 1024px to 2560px viewports, ensure no horizontal scroll, scale cell width and toolbar layout, handle sidebar collapse at narrower widths
- [x] T041 Add markdown cell rendering in `src/static/js/cell.js`: parse markdown source to HTML when cell is not in edit mode (use a lightweight markdown parser), toggle between rendered markdown view and CodeMirror editor on Enter/Esc, support common markdown (headings, bold, italic, code blocks, links, lists)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational phase completion
- **User Story 2 (Phase 4)**: Depends on US1 (extends executor and container)
- **User Story 3 (Phase 5)**: Depends on US1 (extends container creation)
- **User Story 4 (Phase 6)**: Depends on US1 (extends REST API and frontend)
- **Polish (Phase 7)**: Depends on US1 at minimum; ideally after all stories

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2) — No dependencies on other stories
- **User Story 2 (P2)**: Depends on US1 for executor server and container volume infrastructure
- **User Story 3 (P3)**: Depends on US1 for container lifecycle; can run in parallel with US2
- **User Story 4 (P4)**: Depends on US1 for base REST API and frontend; can run in parallel with US2/US3

### Within Each User Story

- Models/config before services
- Services before REST routes
- REST routes before WebSocket
- Backend before frontend
- Core implementation before integration

### Parallel Opportunities

- T003 + T004 (Setup: linting + gitignore)
- T006 + T007 + T010 (Foundational: config + Dockerfile + test fixtures)
- T018 + T019 (US1: HTML page + CSS — different files)
- T025 (US2) runs in parallel with T023/T024
- T029 (US3) runs in parallel with T027/T028
- T030 + T031 + T032 (US4: independent REST routes)
- T036 + T037 + T038 + T039 (Polish: all independent concerns)
- **US3 and US4 can run in parallel** after US1 completes (if team capacity allows)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: Create a notebook, execute `print("hello")`, verify output
5. Deploy/demo if ready — this is a working notebook application

### Incremental Delivery

1. Complete Setup + Foundational → Foundation ready
2. Add User Story 1 → Test independently → Demo (MVP!)
3. Add User Story 2 → Test package install persistence → Demo
4. Add User Story 3 → Test GPU access → Demo
5. Add User Story 4 → Test full CRUD + persistence → Demo
6. Polish → Accessibility, error handling, responsiveness → Release

### Parallel Team Strategy

With multiple developers after Foundational is done:

1. Team completes Setup + Foundational together
2. Everyone works on User Story 1 (most tasks are sequential)
3. Once US1 is done:
   - Developer A: User Story 2 (executor + volumes)
   - Developer B: User Story 3 (GPU passthrough)
   - Developer C: User Story 4 (CRUD + persistence UI)
4. Everyone on Polish phase

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- Avoid: vague tasks, same file conflicts, cross-story dependencies that break independence
