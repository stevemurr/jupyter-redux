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
    hf_token: str | None = None

    # --- Host uid mapping ---
    # Env containers run as this uid/gid instead of root. Files written
    # to host bind mounts (e.g. /shared/artifacts) land with these
    # owners so the host user can touch them without sudo. Default
    # 1000:1000 covers the common single-user Linux case; override in
    # docker-compose .env or the runtime environment for other hosts.
    host_uid: int = 1000
    host_gid: int = 1000

    # --- Shared host bind mounts ---
    # Absolute *host* paths bind-mounted into every env container at
    # /shared/datasets (read-only) and /shared/artifacts (read-write).
    # The backend itself runs inside a container so Path.home() here
    # would resolve to /root, not the host's home — these MUST be
    # provided from the host via JREDUX_DATASETS_PATH and
    # JREDUX_ARTIFACTS_PATH (docker-compose.yml wires them from the
    # host shell's $HOME). None disables the corresponding mount.
    datasets_path: Path | None = None
    artifacts_path: Path | None = None

    model_config = {"env_prefix": "JREDUX_"}


settings = Settings()
