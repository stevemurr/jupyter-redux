from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: Path = Path("data")
    docker_base_image: str = "jupyter-redux-base:latest"
    executor_port: int = 9999
    container_idle_timeout_minutes: int = 30
    gpu_enabled: bool = True
    docker_network: str | None = None

    model_config = {"env_prefix": "JREDUX_"}


settings = Settings()
