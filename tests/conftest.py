import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.models import Cell, CellType, Environment, Notebook


@pytest.fixture()
def tmp_data_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(settings, "data_dir", tmp)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def sample_environment() -> Environment:
    return Environment(
        name="Test Environment",
        python_version="3.11",
        gpu=False,
    )


@pytest.fixture()
def sample_notebook(sample_environment: Environment) -> Notebook:
    return Notebook(
        name="Test Notebook",
        environment_id=sample_environment.id,
        cells=[
            Cell(cell_type=CellType.CODE, source='print("hello")'),
            Cell(cell_type=CellType.MARKDOWN, source="# Title"),
        ],
    )


@pytest.fixture()
def mock_docker_client() -> MagicMock:
    client = MagicMock()
    container = MagicMock()
    container.id = "abc123"
    container.status = "running"
    container.ports = {"9999/tcp": [{"HostPort": "32768"}]}
    client.containers.run.return_value = container
    client.containers.get.return_value = container
    client.volumes.create.return_value = MagicMock()
    return client


@pytest.fixture()
async def async_client(tmp_data_dir: Path) -> AsyncClient:
    from src.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture()
def mock_executor() -> AsyncMock:
    executor = AsyncMock()
    executor.execute.return_value = {
        "type": "result",
        "data": "42",
    }
    return executor
