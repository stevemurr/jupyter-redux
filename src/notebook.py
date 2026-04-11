"""Notebook CRUD operations and JSON file persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.config import settings
from src.models import (
    Cell,
    CellType,
    Notebook,
    NotebookIndex,
    NotebookSummary,
)


class NotebookService:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _notebook_path(self, notebook_id: str) -> Path:
        return self.data_dir / f"{notebook_id}.json"

    def _index_path(self) -> Path:
        return self.data_dir / "index.json"

    def _load_index(self) -> NotebookIndex:
        path = self._index_path()
        if path.exists():
            data = json.loads(path.read_text())
            return NotebookIndex(**data)
        return NotebookIndex()

    def _save_index(self, index: NotebookIndex) -> None:
        path = self._index_path()
        path.write_text(index.model_dump_json(indent=2))

    def _update_index_entry(self, notebook: Notebook) -> None:
        index = self._load_index()
        summary = NotebookSummary(
            id=notebook.id,
            name=notebook.name,
            created_at=notebook.created_at,
            updated_at=notebook.updated_at,
            python_version=notebook.python_version,
            gpu=notebook.gpu,
        )
        # Replace or add
        index.notebooks = [n for n in index.notebooks if n.id != notebook.id]
        index.notebooks.append(summary)
        self._save_index(index)

    def _remove_index_entry(self, notebook_id: str) -> None:
        index = self._load_index()
        index.notebooks = [n for n in index.notebooks if n.id != notebook_id]
        self._save_index(index)

    def create_notebook(
        self,
        name: str = "Untitled Notebook",
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> Notebook:
        notebook = Notebook(
            name=name, python_version=python_version, gpu=gpu
        )
        self.save_notebook(notebook)
        return notebook

    def get_notebook(self, notebook_id: str) -> Notebook | None:
        path = self._notebook_path(notebook_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Notebook(**data)

    def save_notebook(self, notebook: Notebook) -> Notebook:
        notebook.updated_at = datetime.now(UTC)
        path = self._notebook_path(notebook.id)
        path.write_text(notebook.model_dump_json(indent=2))
        self._update_index_entry(notebook)
        return notebook

    def delete_notebook(self, notebook_id: str) -> bool:
        path = self._notebook_path(notebook_id)
        if path.exists():
            path.unlink()
            self._remove_index_entry(notebook_id)
            return True
        return False

    def list_notebooks(self) -> NotebookIndex:
        return self._load_index()

    def add_cell(
        self,
        notebook: Notebook,
        cell_type: CellType = CellType.CODE,
        source: str = "",
        position: int | None = None,
    ) -> Notebook:
        cell = Cell(cell_type=cell_type, source=source)
        if position is not None and 0 <= position <= len(notebook.cells):
            notebook.cells.insert(position, cell)
        else:
            notebook.cells.append(cell)
        self.save_notebook(notebook)
        return notebook

    def update_cell(
        self,
        notebook: Notebook,
        cell_id: str,
        source: str | None = None,
        cell_type: CellType | None = None,
    ) -> Notebook | None:
        for cell in notebook.cells:
            if cell.id == cell_id:
                if source is not None:
                    cell.source = source
                if cell_type is not None:
                    cell.cell_type = cell_type
                self.save_notebook(notebook)
                return notebook
        return None

    def delete_cell(self, notebook: Notebook, cell_id: str) -> Notebook | None:
        original_len = len(notebook.cells)
        notebook.cells = [c for c in notebook.cells if c.id != cell_id]
        if len(notebook.cells) == original_len:
            return None
        self.save_notebook(notebook)
        return notebook

    def reorder_cells(
        self, notebook: Notebook, cell_ids: list[str]
    ) -> Notebook | None:
        existing_ids = {c.id for c in notebook.cells}
        if set(cell_ids) != existing_ids or len(cell_ids) != len(existing_ids):
            return None
        cell_map = {c.id: c for c in notebook.cells}
        notebook.cells = [cell_map[cid] for cid in cell_ids]
        self.save_notebook(notebook)
        return notebook
