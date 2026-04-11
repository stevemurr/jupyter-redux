"""Environment and notebook CRUD operations with JSON file persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.config import settings
from src.models import (
    Cell,
    CellType,
    Environment,
    EnvironmentIndex,
    EnvironmentSummary,
    Notebook,
    NotebookIndex,
    NotebookSummary,
)


class EnvironmentService:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or settings.data_dir
        self.env_dir = self.data_dir / "environments"
        self.nb_dir = self.data_dir / "notebooks"
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.nb_dir.mkdir(parents=True, exist_ok=True)

    # --- Environment persistence ---

    def _env_path(self, env_id: str) -> Path:
        return self.env_dir / f"{env_id}.json"

    def _env_index_path(self) -> Path:
        return self.env_dir / "index.json"

    def _load_env_index(self) -> EnvironmentIndex:
        path = self._env_index_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return EnvironmentIndex(**data)
            except Exception:
                path.unlink(missing_ok=True)
        return EnvironmentIndex()

    def _save_env_index(self, index: EnvironmentIndex) -> None:
        self._env_index_path().write_text(index.model_dump_json(indent=2))

    def _update_env_index_entry(self, env: Environment) -> None:
        index = self._load_env_index()
        notebooks = self.list_notebooks(env.id)
        summary = EnvironmentSummary(
            id=env.id,
            name=env.name,
            python_version=env.python_version,
            gpu=env.gpu,
            notebook_count=len(notebooks.notebooks),
            created_at=env.created_at,
            updated_at=env.updated_at,
        )
        index.environments = [e for e in index.environments if e.id != env.id]
        index.environments.append(summary)
        self._save_env_index(index)

    def _remove_env_index_entry(self, env_id: str) -> None:
        index = self._load_env_index()
        index.environments = [e for e in index.environments if e.id != env_id]
        self._save_env_index(index)

    def create_environment(
        self,
        name: str = "Untitled Environment",
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> Environment:
        env = Environment(name=name, python_version=python_version, gpu=gpu)
        self.save_environment(env)
        return env

    def get_environment(self, env_id: str) -> Environment | None:
        path = self._env_path(env_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Environment(**data)

    def save_environment(self, env: Environment) -> Environment:
        env.updated_at = datetime.now(UTC)
        path = self._env_path(env.id)
        path.write_text(env.model_dump_json(indent=2))
        self._update_env_index_entry(env)
        return env

    def delete_environment(self, env_id: str) -> bool:
        path = self._env_path(env_id)
        if not path.exists():
            return False
        # Delete all notebooks in this environment
        nb_index = self.list_notebooks(env_id)
        for nb_summary in nb_index.notebooks:
            self.delete_notebook(nb_summary.id)
        path.unlink()
        self._remove_env_index_entry(env_id)
        return True

    def list_environments(self) -> EnvironmentIndex:
        return self._load_env_index()

    # --- Notebook persistence ---

    def _notebook_path(self, notebook_id: str) -> Path:
        return self.nb_dir / f"{notebook_id}.json"

    def _nb_index_path(self) -> Path:
        return self.nb_dir / "index.json"

    def _load_nb_index(self) -> NotebookIndex:
        path = self._nb_index_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return NotebookIndex(**data)
            except Exception:
                # Stale index from pre-environment schema; reset it
                path.unlink(missing_ok=True)
        return NotebookIndex()

    def _save_nb_index(self, index: NotebookIndex) -> None:
        self._nb_index_path().write_text(index.model_dump_json(indent=2))

    def _update_nb_index_entry(self, notebook: Notebook) -> None:
        index = self._load_nb_index()
        summary = NotebookSummary(
            id=notebook.id,
            name=notebook.name,
            environment_id=notebook.environment_id,
            created_at=notebook.created_at,
            updated_at=notebook.updated_at,
        )
        index.notebooks = [n for n in index.notebooks if n.id != notebook.id]
        index.notebooks.append(summary)
        self._save_nb_index(index)

    def _remove_nb_index_entry(self, notebook_id: str) -> None:
        index = self._load_nb_index()
        index.notebooks = [n for n in index.notebooks if n.id != notebook_id]
        self._save_nb_index(index)

    def create_notebook(
        self,
        environment_id: str,
        name: str = "Untitled Notebook",
    ) -> Notebook:
        notebook = Notebook(name=name, environment_id=environment_id)
        self.save_notebook(notebook)
        # Update environment index to reflect new notebook count
        env = self.get_environment(environment_id)
        if env:
            self._update_env_index_entry(env)
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
        self._update_nb_index_entry(notebook)
        return notebook

    def delete_notebook(self, notebook_id: str) -> bool:
        notebook = self.get_notebook(notebook_id)
        if notebook is None:
            return False
        env_id = notebook.environment_id
        path = self._notebook_path(notebook_id)
        path.unlink()
        self._remove_nb_index_entry(notebook_id)
        # Update environment index to reflect new notebook count
        env = self.get_environment(env_id)
        if env:
            self._update_env_index_entry(env)
        return True

    def list_notebooks(self, environment_id: str) -> NotebookIndex:
        index = self._load_nb_index()
        filtered = [n for n in index.notebooks if n.environment_id == environment_id]
        return NotebookIndex(notebooks=filtered)

    # --- Cell operations ---

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
