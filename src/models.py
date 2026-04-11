from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CellType(StrEnum):
    CODE = "code"
    MARKDOWN = "markdown"


class OutputType(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    RESULT = "result"
    ERROR = "error"


class ExecutionState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    ERRORED = "errored"


class ContainerStatus(StrEnum):
    NONE = "none"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Output(BaseModel):
    output_type: OutputType
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Cell(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cell_type: CellType = CellType.CODE
    source: str = ""
    outputs: list[Output] = Field(default_factory=list)
    execution_count: int | None = None
    execution_state: ExecutionState = ExecutionState.IDLE


class Notebook(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Untitled Notebook"
    environment_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cells: list[Cell] = Field(default_factory=list)


class Environment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Untitled Environment"
    python_version: str = "3.11"
    gpu: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ContainerState(BaseModel):
    status: ContainerStatus = ContainerStatus.NONE
    container_id: str | None = None
    host_port: int | None = None
    error_message: str | None = None


class NotebookSummary(BaseModel):
    id: str
    name: str
    environment_id: str
    created_at: datetime
    updated_at: datetime


class NotebookIndex(BaseModel):
    notebooks: list[NotebookSummary] = Field(default_factory=list)


class EnvironmentSummary(BaseModel):
    id: str
    name: str
    python_version: str = "3.11"
    gpu: bool = False
    notebook_count: int = 0
    created_at: datetime
    updated_at: datetime


class EnvironmentIndex(BaseModel):
    environments: list[EnvironmentSummary] = Field(default_factory=list)


# --- Request/Response models for the REST API ---


class CreateEnvironmentRequest(BaseModel):
    name: str = "Untitled Environment"
    python_version: str = "3.11"
    gpu: bool = False


class UpdateEnvironmentRequest(BaseModel):
    name: str | None = None


class CreateNotebookRequest(BaseModel):
    name: str = "Untitled Notebook"


class UpdateNotebookRequest(BaseModel):
    name: str | None = None


class AddCellRequest(BaseModel):
    cell_type: CellType = CellType.CODE
    source: str = ""
    position: int | None = None


class UpdateCellRequest(BaseModel):
    source: str | None = None
    cell_type: CellType | None = None


class ReorderCellsRequest(BaseModel):
    cell_ids: list[str]


class EnvironmentResponse(BaseModel):
    id: str
    name: str
    python_version: str
    gpu: bool
    created_at: datetime
    updated_at: datetime
    notebooks: list[NotebookSummary] = Field(default_factory=list)
    container_state: ContainerState = Field(default_factory=ContainerState)


class EnvironmentListResponse(BaseModel):
    environments: list[EnvironmentSummary]


class NotebookResponse(BaseModel):
    id: str
    name: str
    environment_id: str
    created_at: datetime
    updated_at: datetime
    cells: list[Cell]
    container_state: ContainerState = Field(default_factory=ContainerState)


class NotebookListResponse(BaseModel):
    notebooks: list[NotebookSummary]


class ContainerStateResponse(BaseModel):
    status: ContainerStatus
    container_id: str | None = None
    error_message: str | None = None


# --- File operations ---


class FileEntry(BaseModel):
    name: str
    path: str
    type: str  # "file" or "directory"
    size: int | None = None


class FileTreeResponse(BaseModel):
    entries: list[FileEntry]


class FileContentResponse(BaseModel):
    path: str
    content: str


class WriteFileRequest(BaseModel):
    path: str
    content: str


class MkdirRequest(BaseModel):
    path: str


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str
