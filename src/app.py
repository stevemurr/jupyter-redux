from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.models import (
    AddCellRequest,
    ContainerStateResponse,
    CreateNotebookRequest,
    NotebookListResponse,
    NotebookResponse,
    ReorderCellsRequest,
    UpdateCellRequest,
    UpdateNotebookRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start idle monitor on startup
    csvc = _get_container_service()
    await csvc.start_idle_monitor()
    yield
    await csvc.stop_idle_monitor()


app = FastAPI(title="Jupyter Redux", version="0.1.0", lifespan=lifespan)

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


# --- Notebook CRUD ---

# Lazy imports to avoid circular deps at module level
_notebook_service = None
_container_service = None


def _get_notebook_service():
    global _notebook_service
    if _notebook_service is None:
        from src.notebook import NotebookService

        _notebook_service = NotebookService()
    return _notebook_service


def _get_container_service():
    global _container_service
    if _container_service is None:
        from src.container import ContainerService

        _container_service = ContainerService()
    return _container_service


def _build_response(notebook, container_state=None) -> NotebookResponse:
    from src.models import ContainerState

    return NotebookResponse(
        id=notebook.id,
        name=notebook.name,
        created_at=notebook.created_at,
        updated_at=notebook.updated_at,
        cells=notebook.cells,
        container_state=container_state or ContainerState(),
    )


@app.get("/api/notebooks", response_model=NotebookListResponse)
async def list_notebooks() -> NotebookListResponse:
    svc = _get_notebook_service()
    index = svc.list_notebooks()
    return NotebookListResponse(notebooks=index.notebooks)


@app.post("/api/notebooks", response_model=NotebookResponse, status_code=201)
async def create_notebook(req: CreateNotebookRequest) -> NotebookResponse:
    svc = _get_notebook_service()

    # Auto-deduplicate name (like Jupyter: "Untitled Notebook 1", etc.)
    index = svc.list_notebooks()
    existing_names = {nb.name for nb in index.notebooks}
    name = req.name
    counter = 1
    while name in existing_names:
        counter += 1
        name = f"{req.name} {counter}"

    notebook = svc.create_notebook(
        name, python_version=req.python_version, gpu=req.gpu
    )

    # Container provisioning is deferred to WebSocket connection
    return _build_response(notebook)


@app.get("/api/notebooks/{notebook_id}", response_model=NotebookResponse)
async def get_notebook(notebook_id: str) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


@app.put(
    "/api/notebooks/{notebook_id}",
    response_model=NotebookResponse,
)
async def update_notebook(
    notebook_id: str, req: UpdateNotebookRequest
) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")

    if req.name is not None:
        index = svc.list_notebooks()
        for nb in index.notebooks:
            if nb.name == req.name and nb.id != notebook_id:
                raise HTTPException(409, f"Notebook '{req.name}' already exists")
        notebook.name = req.name

    svc.save_notebook(notebook)
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


@app.delete("/api/notebooks/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str) -> None:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    svc.delete_notebook(notebook_id)
    try:
        csvc.destroy_container(notebook_id)
    except Exception:
        pass  # Best-effort: notebook data is already gone


# --- Cell CRUD ---


@app.post(
    "/api/notebooks/{notebook_id}/cells",
    response_model=NotebookResponse,
    status_code=201,
)
async def add_cell(notebook_id: str, req: AddCellRequest) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.add_cell(notebook, req.cell_type, req.source, req.position)
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


@app.put(
    "/api/notebooks/{notebook_id}/cells/{cell_id}",
    response_model=NotebookResponse,
)
async def update_cell(
    notebook_id: str, cell_id: str, req: UpdateCellRequest
) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.update_cell(notebook, cell_id, req.source, req.cell_type)
    if notebook is None:
        raise HTTPException(404, "Cell not found")
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


@app.delete(
    "/api/notebooks/{notebook_id}/cells/{cell_id}",
    response_model=NotebookResponse,
)
async def delete_cell(notebook_id: str, cell_id: str) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.delete_cell(notebook, cell_id)
    if notebook is None:
        raise HTTPException(404, "Cell not found")
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


@app.post(
    "/api/notebooks/{notebook_id}/cells/reorder",
    response_model=NotebookResponse,
)
async def reorder_cells(notebook_id: str, req: ReorderCellsRequest) -> NotebookResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    notebook = svc.reorder_cells(notebook, req.cell_ids)
    if notebook is None:
        raise HTTPException(400, "Cell IDs do not match existing cells")
    container_state = csvc.get_container_status(notebook_id)
    return _build_response(notebook, container_state)


# --- Container Management ---


@app.post("/api/notebooks/{notebook_id}/container/start")
async def start_container(notebook_id: str) -> ContainerStateResponse:
    svc = _get_notebook_service()
    csvc = _get_container_service()
    notebook = svc.get_notebook(notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    state = csvc.start_container(notebook_id)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


@app.post("/api/notebooks/{notebook_id}/container/stop")
async def stop_container(notebook_id: str) -> ContainerStateResponse:
    csvc = _get_container_service()
    state = csvc.stop_container(notebook_id)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


@app.get("/api/notebooks/{notebook_id}/container/status")
async def container_status(notebook_id: str) -> ContainerStateResponse:
    csvc = _get_container_service()
    state = csvc.get_container_status(notebook_id)
    return ContainerStateResponse(
        status=state.status,
        container_id=state.container_id,
        error_message=state.error_message,
    )


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


@app.get("/notebook/{notebook_id}")
async def serve_notebook_page(notebook_id: str) -> FileResponse:
    return FileResponse(
        str(static_dir / "notebook.html"),
        headers={"Cache-Control": "no-store"},
    )


# Register WebSocket routes (must be after app is defined)
import src.websocket  # noqa: E402, F401
