import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.models import (
    AddCellRequest,
    CloneRepoRequest,
    CloneRepoResponse,
    ContainerState,
    ContainerStateResponse,
    CreateEnvironmentRequest,
    CreateNotebookRequest,
    EnvironmentListResponse,
    EnvironmentResponse,
    FileContentResponse,
    FileEntry,
    FileTreeResponse,
    InstallRepoRequest,
    MkdirRequest,
    NotebookResponse,
    ProjectDetection,
    RenameFileRequest,
    ReorderCellsRequest,
    RepoListResponse,
    RepoSummary,
    UpdateCellRequest,
    UpdateEnvironmentRequest,
    UpdateNotebookRequest,
    WriteFileRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reset any cells that were left in the "running" state from a
    # previous backend process — the ExecutionManager is in-memory so
    # those executions can't actually still be in flight.
    from src.environment import EnvironmentService
    stale = EnvironmentService().clear_stale_running_cells()
    if stale:
        import logging
        logging.getLogger(__name__).info(
            "Reset %d stale running cell(s) to errored on startup", stale,
        )

    csvc = _get_container_service()
    await csvc.start_idle_monitor()
    yield
    await csvc.stop_idle_monitor()


app = FastAPI(title="Jupyter Redux", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    error_map = {
        "ConnectionRefusedError": (
            503,
            "Docker is not running. Start Docker and try again.",
        ),
        "DockerException": (
            503,
            "Docker encountered an error. Check that Docker is running.",
        ),
        "FileNotFoundError": (
            404,
            "The requested resource was not found.",
        ),
    }
    exc_name = type(exc).__name__
    status, message = error_map.get(exc_name, (500, f"Internal error: {exc}"))
    return JSONResponse(status_code=status, content={"detail": message})


# --- Health ---


@app.get("/api/health")
async def health_check() -> dict:
    return {"status": "ok"}


# --- Lazy service singletons ---

_env_service = None
_container_service = None


def _get_env_service():
    global _env_service
    if _env_service is None:
        from src.environment import EnvironmentService

        _env_service = EnvironmentService()
    return _env_service


def _get_container_service():
    global _container_service
    if _container_service is None:
        from src.container import ContainerService

        _container_service = ContainerService()
    return _container_service


# --- Environment CRUD ---


def _build_env_response(
    env, notebooks=None, container_state=None,
) -> EnvironmentResponse:
    return EnvironmentResponse(
        id=env.id,
        name=env.name,
        python_version=env.python_version,
        gpu=env.gpu,
        created_at=env.created_at,
        updated_at=env.updated_at,
        notebooks=notebooks or [],
        container_state=container_state or ContainerState(),
    )


@app.get("/api/environments", response_model=EnvironmentListResponse)
async def list_environments() -> EnvironmentListResponse:
    svc = _get_env_service()
    index = svc.list_environments()
    return EnvironmentListResponse(environments=index.environments)


@app.post("/api/environments", response_model=EnvironmentResponse, status_code=201)
async def create_environment(req: CreateEnvironmentRequest) -> EnvironmentResponse:
    svc = _get_env_service()

    # Auto-deduplicate name
    index = svc.list_environments()
    existing_names = {e.name for e in index.environments}
    name = req.name
    counter = 1
    while name in existing_names:
        counter += 1
        name = f"{req.name} {counter}"

    env = svc.create_environment(
        name, python_version=req.python_version, gpu=req.gpu
    )

    # Auto-create a first notebook so the user has something to open
    svc.create_notebook(env.id, "Notebook 1")
    notebooks = svc.list_notebooks(env.id)
    return _build_env_response(env, notebooks.notebooks)


@app.get("/api/environments/{env_id}", response_model=EnvironmentResponse)
async def get_environment(env_id: str) -> EnvironmentResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    notebooks = svc.list_notebooks(env_id)
    container_state = csvc.get_container_status(env_id)
    return _build_env_response(env, notebooks.notebooks, container_state)


@app.put("/api/environments/{env_id}", response_model=EnvironmentResponse)
async def update_environment(
    env_id: str, req: UpdateEnvironmentRequest
) -> EnvironmentResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")

    if req.name is not None:
        index = svc.list_environments()
        for e in index.environments:
            if e.name == req.name and e.id != env_id:
                raise HTTPException(409, f"Environment '{req.name}' already exists")
        env.name = req.name

    svc.save_environment(env)
    notebooks = svc.list_notebooks(env_id)
    container_state = csvc.get_container_status(env_id)
    return _build_env_response(env, notebooks.notebooks, container_state)



@app.delete("/api/environments/{env_id}", status_code=204)
async def delete_environment(env_id: str) -> None:
    svc = _get_env_service()
    csvc = _get_container_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    svc.delete_environment(env_id)
    try:
        csvc.destroy_container(env_id)
    except Exception:
        pass


# --- Container Management (environment level) ---


@app.post("/api/environments/{env_id}/container/start")
async def start_container(env_id: str) -> ContainerStateResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    state = csvc.start_container(env_id, env.python_version, env.gpu)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


@app.post("/api/environments/{env_id}/container/stop")
async def stop_container(env_id: str) -> ContainerStateResponse:
    svc = _get_env_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    csvc = _get_container_service()
    state = csvc.stop_container(env_id)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


@app.get("/api/environments/{env_id}/container/status")
async def container_status(env_id: str) -> ContainerStateResponse:
    csvc = _get_container_service()
    state = csvc.get_container_status(env_id)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


# --- File Operations ---


def _require_running_env(env_id: str):
    """Validate env exists and container is running."""
    from src.models import ContainerStatus

    svc = _get_env_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")
    csvc = _get_container_service()
    state = csvc.get_container_status(env_id)
    if state.status != ContainerStatus.READY:
        raise HTTPException(
            409,
            "Container is not running. Start the environment first.",
        )
    return csvc


@app.get(
    "/api/environments/{env_id}/files",
    response_model=FileTreeResponse,
)
async def list_files(env_id: str) -> FileTreeResponse:
    csvc = _require_running_env(env_id)
    entries = csvc.list_files(env_id)
    return FileTreeResponse(
        entries=[FileEntry(**e) for e in entries],
    )


@app.get(
    "/api/environments/{env_id}/files/read",
    response_model=FileContentResponse,
)
async def read_file(env_id: str, path: str) -> FileContentResponse:
    csvc = _require_running_env(env_id)
    content = csvc.read_file(env_id, path)
    return FileContentResponse(path=path, content=content)


@app.put("/api/environments/{env_id}/files/write")
async def write_file(
    env_id: str, req: WriteFileRequest,
) -> FileContentResponse:
    csvc = _require_running_env(env_id)
    csvc.write_file(env_id, req.path, req.content)
    return FileContentResponse(path=req.path, content=req.content)


@app.delete("/api/environments/{env_id}/files", status_code=204)
async def delete_file(env_id: str, path: str) -> None:
    csvc = _require_running_env(env_id)
    csvc.delete_file(env_id, path)


@app.post("/api/environments/{env_id}/files/mkdir", status_code=201)
async def make_directory(
    env_id: str, req: MkdirRequest,
) -> dict:
    csvc = _require_running_env(env_id)
    csvc.make_directory(env_id, req.path)
    return {"path": req.path}


@app.post("/api/environments/{env_id}/files/rename")
async def rename_file(
    env_id: str, req: RenameFileRequest,
) -> dict:
    csvc = _require_running_env(env_id)
    csvc.rename_file(env_id, req.old_path, req.new_path)
    return {"old_path": req.old_path, "new_path": req.new_path}


# --- Shared dir browsing (read-only) ---

_SHARED_ROOTS = {
    "datasets": Path("/shared/datasets"),
    "artifacts": Path("/shared/artifacts"),
}


@app.get("/api/shared/{root}", response_model=FileTreeResponse)
async def list_shared_tree(root: str) -> FileTreeResponse:
    """Walk /shared/datasets or /shared/artifacts and return the tree.

    These host directories are bind-mounted into the backend container
    read-only, so listing happens directly without going through an
    env container. That means users can browse them even when no env
    container is running.
    """
    base = _SHARED_ROOTS.get(root)
    if base is None:
        raise HTTPException(
            status_code=400,
            detail="root must be 'datasets' or 'artifacts'",
        )
    if not base.exists():
        return FileTreeResponse(entries=[])

    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel = os.path.relpath(dirpath, base)
        rel = "" if rel == "." else rel
        dirnames.sort()
        filenames.sort()
        for d in dirnames:
            p = f"{rel}/{d}" if rel else d
            entries.append(FileEntry(name=d, path=p, type="directory"))
        for f in filenames:
            p = f"{rel}/{f}" if rel else f
            try:
                sz = (Path(dirpath) / f).stat().st_size
            except OSError:
                sz = 0
            entries.append(
                FileEntry(name=f, path=p, type="file", size=sz),
            )
    return FileTreeResponse(entries=entries)


# --- Repo Operations ---


@app.get(
    "/api/environments/{env_id}/repos",
    response_model=RepoListResponse,
)
async def list_repos(env_id: str) -> RepoListResponse:
    csvc = _require_running_env(env_id)
    repos = csvc.list_repos(env_id)
    return RepoListResponse(
        repos=[RepoSummary(**r) for r in repos],
    )


@app.post(
    "/api/environments/{env_id}/repos/clone",
    response_model=CloneRepoResponse,
    status_code=201,
)
async def clone_repo(
    env_id: str, req: CloneRepoRequest,
) -> CloneRepoResponse:
    csvc = _require_running_env(env_id)
    result = csvc.clone_repo(
        env_id, req.url, req.branch, req.name,
    )
    return CloneRepoResponse(
        name=result["name"],
        path=result["path"],
        url=result["url"],
        branch=result["branch"],
        clone_output=result["clone_output"],
        project=ProjectDetection(**result["project"]),
    )


@app.post("/api/environments/{env_id}/repos/install")
async def install_repo(
    env_id: str, req: InstallRepoRequest,
) -> dict:
    csvc = _require_running_env(env_id)
    output = csvc.install_repo(env_id, req.repo_name)
    return {"repo_name": req.repo_name, "output": output}


@app.get("/api/environments/{env_id}/repos/install-stream")
async def install_repo_stream(
    env_id: str, repo_name: str,
) -> StreamingResponse:
    csvc = _require_running_env(env_id)
    return StreamingResponse(
        csvc.install_repo_stream(env_id, repo_name),
        media_type="text/plain",
    )


@app.get("/api/environments/{env_id}/repos/pull-stream")
async def pull_repo_stream(
    env_id: str, repo_name: str,
) -> StreamingResponse:
    csvc = _require_running_env(env_id)
    return StreamingResponse(
        csvc.pull_repo_stream(env_id, repo_name),
        media_type="text/plain",
    )


@app.delete(
    "/api/environments/{env_id}/repos/{repo_name}",
    status_code=204,
)
async def delete_repo(env_id: str, repo_name: str) -> None:
    csvc = _require_running_env(env_id)
    csvc.delete_repo(env_id, repo_name)


# --- Notebook CRUD ---


def _build_notebook_response(notebook, container_state=None) -> NotebookResponse:
    return NotebookResponse(
        id=notebook.id,
        name=notebook.name,
        environment_id=notebook.environment_id,
        created_at=notebook.created_at,
        updated_at=notebook.updated_at,
        cells=notebook.cells,
        container_state=container_state or ContainerState(),
    )


@app.post(
    "/api/environments/{env_id}/notebooks",
    response_model=NotebookResponse,
    status_code=201,
)
async def create_notebook(env_id: str, req: CreateNotebookRequest) -> NotebookResponse:
    svc = _get_env_service()
    env = svc.get_environment(env_id)
    if env is None:
        raise HTTPException(404, "Environment not found")

    # Auto-deduplicate name within this environment
    nb_index = svc.list_notebooks(env_id)
    existing_names = {nb.name for nb in nb_index.notebooks}
    name = req.name
    counter = 1
    while name in existing_names:
        counter += 1
        name = f"{req.name} {counter}"

    notebook = svc.create_notebook(env_id, name)
    container_state = _get_container_service().get_container_status(env_id)
    return _build_notebook_response(notebook, container_state)


@app.get("/api/notebooks/{notebook_id}", response_model=NotebookResponse)
async def get_notebook(notebook_id: str) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


@app.put("/api/notebooks/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: str, req: UpdateNotebookRequest
) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")

    if req.name is not None:
        nb_index = svc.list_notebooks(notebook.environment_id)
        for nb in nb_index.notebooks:
            if nb.name == req.name and nb.id != notebook_id:
                raise HTTPException(409, f"Notebook '{req.name}' already exists")
        notebook.name = req.name

    svc.save_notebook(notebook)
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


@app.delete("/api/notebooks/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str) -> None:
    svc = _get_env_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    svc.delete_notebook(notebook_id)


# --- Cell CRUD ---


@app.post(
    "/api/notebooks/{notebook_id}/cells",
    response_model=NotebookResponse,
    status_code=201,
)
async def add_cell(notebook_id: str, req: AddCellRequest) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.add_cell(notebook, req.cell_type, req.source, req.position)
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


@app.put(
    "/api/notebooks/{notebook_id}/cells/{cell_id}",
    response_model=NotebookResponse,
)
async def update_cell(
    notebook_id: str, cell_id: str, req: UpdateCellRequest
) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.update_cell(notebook, cell_id, req.source, req.cell_type)
    if notebook is None:
        raise HTTPException(404, "Cell not found")
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


@app.delete(
    "/api/notebooks/{notebook_id}/cells/{cell_id}",
    response_model=NotebookResponse,
)
async def delete_cell(notebook_id: str, cell_id: str) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.delete_cell(notebook, cell_id)
    if notebook is None:
        raise HTTPException(404, "Cell not found")
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


@app.post(
    "/api/notebooks/{notebook_id}/cells/reorder",
    response_model=NotebookResponse,
)
async def reorder_cells(notebook_id: str, req: ReorderCellsRequest) -> NotebookResponse:
    svc = _get_env_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.reorder_cells(notebook, req.cell_ids)
    if notebook is None:
        raise HTTPException(400, "Cell IDs do not match existing cells")
    container_state = csvc.get_container_status(notebook.environment_id)
    return _build_notebook_response(notebook, container_state)


# --- Static files / SPA ---

static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(str(static_dir / "index.html"))


@app.get("/environment/{env_id}")
async def serve_environment_page(env_id: str) -> FileResponse:
    return FileResponse(
        str(static_dir / "environment.html"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/environment/{env_id}/notebook/{notebook_id}")
async def serve_notebook_page(env_id: str, notebook_id: str) -> FileResponse:
    return FileResponse(
        str(static_dir / "notebook.html"),
        headers={"Cache-Control": "no-store"},
    )


# Register WebSocket routes (must be after app is defined)
import src.websocket  # noqa: E402, F401
