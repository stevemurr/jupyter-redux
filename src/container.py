"""Docker container lifecycle management for environments."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import posixpath
import tarfile
import time

import docker
import docker.errors
import docker.types

from src.config import settings
from src.models import ContainerState, ContainerStatus

FILES_ROOT = "/env/files"

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "jredux-env"
VOLUME_PREFIX = "jredux-env"

# Named docker volumes for tool caches shared across all env containers.
# Mounted at the target user's ~/.cache path so HuggingFace, pip, torch,
# and uv find their cache without extra env vars.
CACHE_HF_VOLUME = "jredux-cache-hf"
CACHE_PIP_VOLUME = "jredux-cache-pip"
CACHE_TORCH_VOLUME = "jredux-cache-torch"
CACHE_UV_VOLUME = "jredux-cache-uv"


def _parse_nvidia_smi_csv(text: str) -> tuple[int, int, int, bool, bool]:
    """Parse nvidia-smi CSV (memory.used, memory.total, utilization.gpu).

    Returns (used_mib, total_mib, max_util_pct, mem_supported, saw_any_line).
    ``mem_supported`` is False on unified-memory devices (e.g. GB10)
    where memory columns come back as ``[N/A]``.
    """
    used_mib = 0
    total_mib = 0
    mem_supported = False
    max_util = 0
    saw_any_line = False
    for raw in text.splitlines():
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            continue
        saw_any_line = True
        try:
            util = int(parts[2])
            if util > max_util:
                max_util = util
        except ValueError:
            pass
        try:
            used_mib += int(parts[0])
            total_mib += int(parts[1])
            mem_supported = True
        except ValueError:
            pass
    return used_mib, total_mib, max_util, mem_supported, saw_any_line


def _compute_cpu_pct(stats: dict) -> float:
    """CPU% from a single docker stats snapshot, normalized to 0–100.

    The snapshot includes both ``cpu_stats`` and ``precpu_stats``; the
    delta between them covers roughly the sampling window. 100 = every
    online core fully pegged.
    """
    cpu = stats.get("cpu_stats") or {}
    pre = stats.get("precpu_stats") or {}
    cpu_delta = (
        (cpu.get("cpu_usage") or {}).get("total_usage", 0)
        - (pre.get("cpu_usage") or {}).get("total_usage", 0)
    )
    system_delta = (
        cpu.get("system_cpu_usage", 0)
        - pre.get("system_cpu_usage", 0)
    )
    if system_delta <= 0 or cpu_delta <= 0:
        return 0.0
    return (cpu_delta / system_delta) * 100.0


class ContainerService:
    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None
        self._last_activity: dict[str, float] = {}
        self._idle_task: asyncio.Task | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def get_container_name(self, environment_id: str) -> str:
        return f"{CONTAINER_PREFIX}-{environment_id}"

    def _volume_name(self, environment_id: str) -> str:
        return f"{VOLUME_PREFIX}-{environment_id}"

    def get_image_tag(
        self,
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> str:
        suffix = "-gpu" if gpu else ""
        return f"jupyter-redux-base:py{python_version}{suffix}"

    def has_image(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
            return True
        except docker.errors.ImageNotFound:
            return False

    def build_image_streaming(
        self,
        python_version: str = "3.11",
        gpu: bool = False,
    ):
        """Build an executor image, returning (tag, log_stream).

        The stream yields dicts with build log entries as the build
        progresses. Caller must consume the stream to completion.
        """
        tag = self.get_image_tag(python_version, gpu)
        target = "gpu" if gpu else "cpu"

        logger.info("Building image %s ...", tag)
        api = self.client.api
        stream = api.build(
            path=".",
            dockerfile="src/executor/Dockerfile",
            tag=tag,
            target=target,
            buildargs={"PYTHON_VERSION": python_version},
            rm=True,
            decode=True,
        )
        return tag, stream

    def _ensure_volume(self, vol_name: str) -> None:
        try:
            self.client.volumes.get(vol_name)
        except docker.errors.NotFound:
            self.client.volumes.create(vol_name)

    def _build_env_vars(self) -> dict:
        env_vars = {
            "PYTHONPATH": "/env/lib:/env/files",
            "JREDUX_HOST_UID": str(settings.host_uid),
            "JREDUX_HOST_GID": str(settings.host_gid),
        }
        if settings.hf_token:
            env_vars["HF_TOKEN"] = settings.hf_token
        return env_vars

    def _build_volumes(self, env_volume: str) -> dict:
        """Assemble the volume/bind mount mapping for a fresh env container.

        Per-env volume + tool caches as named docker volumes, plus
        shared host bind mounts for datasets (read-only) and artifacts
        (read-write). Missing host paths are skipped silently.
        """
        for vol in (
            CACHE_HF_VOLUME, CACHE_PIP_VOLUME,
            CACHE_TORCH_VOLUME, CACHE_UV_VOLUME,
        ):
            self._ensure_volume(vol)

        volumes: dict = {
            env_volume: {"bind": "/env", "mode": "rw"},
            CACHE_HF_VOLUME: {
                "bind": "/home/jredux/.cache/huggingface", "mode": "rw",
            },
            CACHE_PIP_VOLUME: {
                "bind": "/home/jredux/.cache/pip", "mode": "rw",
            },
            CACHE_TORCH_VOLUME: {
                "bind": "/home/jredux/.cache/torch", "mode": "rw",
            },
            CACHE_UV_VOLUME: {
                "bind": "/home/jredux/.cache/uv", "mode": "rw",
            },
        }

        if settings.datasets_path is not None:
            host_ds = str(settings.datasets_path)
            volumes[host_ds] = {"bind": "/shared/datasets", "mode": "ro"}

        if settings.artifacts_path is not None:
            host_art = str(settings.artifacts_path)
            volumes[host_art] = {"bind": "/shared/artifacts", "mode": "rw"}

        return volumes

    def create_container(
        self,
        environment_id: str,
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> ContainerState:
        name = self.get_container_name(environment_id)
        vol_name = self._volume_name(environment_id)

        try:
            self._ensure_volume(vol_name)

            image = self.get_image_tag(python_version, gpu)

            run_kwargs: dict = {
                "image": image,
                "name": name,
                "detach": True,
                "shm_size": "8g",
                "volumes": self._build_volumes(vol_name),
                "environment": self._build_env_vars(),
            }

            if settings.docker_network:
                run_kwargs["network"] = settings.docker_network
            else:
                run_kwargs["ports"] = {
                    f"{settings.executor_port}/tcp": None,
                }

            if gpu and settings.gpu_enabled:
                try:
                    run_kwargs["device_requests"] = [
                        docker.types.DeviceRequest(
                            count=-1, capabilities=[["gpu"]]
                        )
                    ]
                except Exception:
                    logger.warning(
                        "GPU requested but NVIDIA runtime not available. "
                        "Creating container without GPU."
                    )

            container = self.client.containers.run(**run_kwargs)
            container.reload()
            host_port = self._get_host_port(container)

            return ContainerState(
                status=ContainerStatus.READY,
                container_id=container.id,
                host_port=host_port,
            )

        except docker.errors.ImageNotFound:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=(
                    f"Base image '{settings.docker_base_image}' not found. "
                    "Build it with: docker build -t jupyter-redux-base:latest "
                    "-f src/executor/Dockerfile ."
                ),
            )
        except docker.errors.APIError as e:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=f"Docker error: {e.explanation}",
            )

    def start_container(
        self,
        environment_id: str,
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> ContainerState:
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
            if container.status != "running":
                container.start()
                container.reload()
            host_port = self._get_host_port(container)
            return ContainerState(
                status=ContainerStatus.READY,
                container_id=container.id,
                host_port=host_port,
            )
        except docker.errors.NotFound:
            return self.create_container(
                environment_id, python_version, gpu
            )
        except docker.errors.APIError as e:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=f"Failed to start container: {e.explanation}",
            )

    def stop_container(self, environment_id: str) -> ContainerState:
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
            container.stop(timeout=10)
            return ContainerState(
                status=ContainerStatus.STOPPED,
                container_id=container.id,
            )
        except docker.errors.NotFound:
            return ContainerState(status=ContainerStatus.NONE)
        except docker.errors.APIError as e:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=f"Failed to stop container: {e.explanation}",
            )

    def restart_container(self, environment_id: str) -> ContainerState:
        """Hard-restart the env container (SIGKILL + start).

        Used for 'Force Stop' — when a cell's user code is stuck in a
        C-bound call that the cooperative interrupt can't reach. The
        timeout=0 bypasses the SIGTERM grace period and jumps straight
        to SIGKILL so we stop immediately. All notebook namespace is
        lost; the caller is responsible for telling the user.
        """
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
            container.restart(timeout=0)
            container.reload()
            host_port = self._get_host_port(container)
            return ContainerState(
                status=ContainerStatus.READY,
                container_id=container.id,
                host_port=host_port,
            )
        except docker.errors.NotFound:
            return ContainerState(status=ContainerStatus.NONE)
        except docker.errors.APIError as e:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=f"Failed to restart container: {e.explanation}",
            )

    def destroy_container(self, environment_id: str) -> None:
        name = self.get_container_name(environment_id)
        vol_name = self._volume_name(environment_id)

        try:
            container = self.client.containers.get(name)
            container.stop(timeout=5)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError:
            logger.warning("Failed to remove container %s", name)

        try:
            volume = self.client.volumes.get(vol_name)
            volume.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError:
            logger.warning("Failed to remove volume %s", vol_name)

    def get_container_status(self, environment_id: str) -> ContainerState:
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
            container.reload()
            if container.status == "running":
                host_port = self._get_host_port(container)
                return ContainerState(
                    status=ContainerStatus.READY,
                    container_id=container.id,
                    host_port=host_port,
                )
            return ContainerState(
                status=ContainerStatus.STOPPED,
                container_id=container.id,
            )
        except docker.errors.NotFound:
            return ContainerState(status=ContainerStatus.NONE)
        except docker.errors.APIError as e:
            return ContainerState(
                status=ContainerStatus.ERROR,
                error_message=str(e),
            )

    def _get_host_port(self, container) -> int | None:
        port_key = f"{settings.executor_port}/tcp"
        ports = container.ports or {}
        bindings = ports.get(port_key)
        if bindings and len(bindings) > 0:
            return int(bindings[0]["HostPort"])
        return None

    # --- File operations (docker exec) ---

    def _get_running_container(self, environment_id: str):
        """Get a running container or raise."""
        name = self.get_container_name(environment_id)
        container = self.client.containers.get(name)
        if container.status != "running":
            raise RuntimeError("Container is not running")
        return container

    def _safe_path(self, rel_path: str) -> str:
        """Resolve a relative path under FILES_ROOT, rejecting traversal."""
        cleaned = posixpath.normpath(rel_path.strip("/"))
        if cleaned == "." or not cleaned:
            return FILES_ROOT
        if cleaned.startswith("..") or "/../" in cleaned:
            raise ValueError("Path traversal not allowed")
        return f"{FILES_ROOT}/{cleaned}"

    def list_files(self, environment_id: str) -> list[dict]:
        """List the file tree under /env/files/ as JSON."""
        container = self._get_running_container(environment_id)
        script = (
            "import os, json\n"
            f"root = '{FILES_ROOT}'\n"
            "os.makedirs(root, exist_ok=True)\n"
            "entries = []\n"
            "for dirpath, dirnames, filenames in os.walk(root):\n"
            "    rel = os.path.relpath(dirpath, root)\n"
            "    if rel == '.': rel = ''\n"
            "    for d in sorted(dirnames):\n"
            "        p = os.path.join(rel, d) if rel else d\n"
            "        entries.append({'name': d, 'path': p,"
            " 'type': 'directory'})\n"
            "    for f in sorted(filenames):\n"
            "        p = os.path.join(rel, f) if rel else f\n"
            "        fp = os.path.join(dirpath, f)\n"
            "        try:\n"
            "            sz = os.path.getsize(fp)\n"
            "        except OSError:\n"
            "            sz = 0\n"
            "        entries.append({'name': f, 'path': p,"
            " 'type': 'file', 'size': sz})\n"
            "print(json.dumps(entries))\n"
        )
        exit_code, output = container.exec_run(
            ["python", "-c", script],
        )
        if exit_code != 0:
            raise RuntimeError(
                f"list_files failed: {output.decode(errors='replace')}"
            )
        return json.loads(output.decode())

    def read_file(self, environment_id: str, path: str) -> str:
        """Read a file from the container filesystem."""
        container = self._get_running_container(environment_id)
        full = self._safe_path(path)
        exit_code, output = container.exec_run(["cat", full])
        if exit_code != 0:
            raise FileNotFoundError(
                f"File not found: {path}"
            )
        return output.decode("utf-8", errors="replace")

    def write_file(
        self, environment_id: str, path: str, content: str,
    ) -> None:
        """Write a file into the container via tar archive."""
        container = self._get_running_container(environment_id)
        full = self._safe_path(path)

        # Ensure parent directory exists
        parent = posixpath.dirname(full)
        container.exec_run(["mkdir", "-p", parent])

        # Build tar in memory with the single file
        data = content.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=posixpath.basename(full))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        container.put_archive(parent, buf)

    def delete_file(self, environment_id: str, path: str) -> None:
        """Delete a file or directory from the container."""
        container = self._get_running_container(environment_id)
        full = self._safe_path(path)
        if full == FILES_ROOT:
            raise ValueError("Cannot delete the files root")
        exit_code, output = container.exec_run(["rm", "-rf", full])
        if exit_code != 0:
            raise RuntimeError(
                f"delete failed: {output.decode(errors='replace')}"
            )

    def make_directory(self, environment_id: str, path: str) -> None:
        """Create a directory in the container."""
        container = self._get_running_container(environment_id)
        full = self._safe_path(path)
        exit_code, output = container.exec_run(["mkdir", "-p", full])
        if exit_code != 0:
            raise RuntimeError(
                f"mkdir failed: {output.decode(errors='replace')}"
            )

    def rename_file(
        self, environment_id: str, old_path: str, new_path: str,
    ) -> None:
        """Rename/move a file or directory."""
        container = self._get_running_container(environment_id)
        old_full = self._safe_path(old_path)
        new_full = self._safe_path(new_path)
        exit_code, output = container.exec_run(
            ["mv", old_full, new_full],
        )
        if exit_code != 0:
            raise RuntimeError(
                f"rename failed: {output.decode(errors='replace')}"
            )

    # --- Repo operations ---

    REPOS_ROOT = "/env/repos"

    def clone_repo(
        self,
        environment_id: str,
        url: str,
        branch: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Clone a git repo into /env/repos/{name}.

        Returns dict with clone result and project detection info.
        """
        container = self._get_running_container(environment_id)

        # Derive repo name from URL if not provided
        if not name:
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]

        dest = f"{self.REPOS_ROOT}/{name}"

        # Check if already exists
        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code == 0:
            raise FileExistsError(
                f"Repo '{name}' already exists at {dest}"
            )

        # Run git clone as jredux so every file lands owned by the
        # host user. Cloning as root (the exec_run default) would
        # leave a root-owned source tree, which then breaks `uv add
        # --editable` because setuptools can't write the package's
        # egg-info back into the source dir.
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([url, dest])

        exit_code, output = container.exec_run(
            cmd,
            user="jredux",
            environment={"HOME": "/home/jredux"},
        )
        if exit_code != 0:
            raise RuntimeError(
                f"git clone failed: {output.decode(errors='replace')}"
            )

        # Detect project type
        detection = self._detect_project(container, dest)

        return {
            "name": name,
            "path": dest,
            "url": url,
            "branch": branch,
            "clone_output": output.decode(errors="replace"),
            "project": detection,
        }

    def _detect_project(
        self, container, repo_path: str,
    ) -> dict:
        """Scan a cloned repo for Python project files."""
        markers = [
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
        ]
        found = []
        for marker in markers:
            exit_code, _ = container.exec_run(
                ["test", "-f", f"{repo_path}/{marker}"],
            )
            if exit_code == 0:
                found.append(marker)

        installable = any(
            m in found
            for m in ("pyproject.toml", "setup.py", "setup.cfg")
        )

        return {
            "markers_found": found,
            "installable": installable,
        }

    def install_repo(
        self, environment_id: str, repo_name: str,
    ) -> str:
        """Install a cloned repo as an editable package via uv add.

        Runs with cwd=/env/files so uv modifies the env's pyproject.toml
        (not the repo's own pyproject.toml, if it has one).
        """
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"

        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code != 0:
            raise FileNotFoundError(f"Repo '{repo_name}' not found")

        exit_code, output = container.exec_run(
            ["uv", "add", "--editable", dest],
            workdir="/env/files",
            user="jredux",
            environment={
                "HOME": "/home/jredux",
                "UV_PROJECT_ENVIRONMENT": "/env/.venv",
                "UV_LINK_MODE": "copy",
            },
        )
        return output.decode(errors="replace")

    def install_repo_stream(
        self, environment_id: str, repo_name: str,
    ):
        """Stream `uv add --editable <repo>` output as it arrives."""
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"

        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code != 0:
            raise FileNotFoundError(f"Repo '{repo_name}' not found")

        _, stream = container.exec_run(
            ["uv", "add", "--editable", dest],
            workdir="/env/files",
            user="jredux",
            environment={
                "HOME": "/home/jredux",
                "UV_PROJECT_ENVIRONMENT": "/env/.venv",
                "UV_LINK_MODE": "copy",
            },
            stream=True,
        )
        for chunk in stream:
            yield chunk.decode(errors="replace")

    def format_code(
        self, environment_id: str, code: str,
    ) -> tuple[str, str | None]:
        """Run `ruff format` on a code snippet inside the env container.

        Writes the code to /tmp/jredux-format.py via put_archive, runs
        `ruff format <file>` as jredux, reads the result back, and
        cleans up. Returns ``(formatted_code, None)`` on success or
        ``(original_code, error_message)`` on failure (syntax error,
        ruff not installed, etc.) so the caller can leave the cell
        content untouched and surface the error to the user.
        """
        container = self._get_running_container(environment_id)

        tmp_name = "jredux-format.py"
        tmp_path = f"/tmp/{tmp_name}"

        # --- 1. Put the code into /tmp in the container. ---
        # Set uid/gid in the tar entry so the file lands owned by
        # jredux (not root, which is what put_archive defaults to
        # because TarInfo.uid/gid are 0 out of the box). Without
        # this, ruff can read the file but can't overwrite it with
        # the formatted result, and the whole format call errors.
        data = code.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=tmp_name)
            info.size = len(data)
            info.mode = 0o644
            info.uid = settings.host_uid
            info.gid = settings.host_gid
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        container.put_archive("/tmp", buf)

        # --- 2. Run ruff format as jredux (so file ownership stays
        #        consistent with the rest of the env).               ---
        # `--no-cache` sidesteps ruff's default of placing the cache
        # directory at <project root>/.ruff_cache. docker exec runs
        # with cwd=/ by default, so ruff walks up to the filesystem
        # root and tries to create /.ruff_cache — which jredux can't
        # write. Single-file formatting doesn't benefit from caching
        # anyway, so turning it off is free.
        exit_code, output = container.exec_run(
            ["ruff", "format", "--no-cache", tmp_path],
            user="jredux",
            environment={"HOME": "/home/jredux"},
        )
        if exit_code != 0:
            container.exec_run(["rm", "-f", tmp_path])
            err = output.decode("utf-8", errors="replace").strip()
            return code, err or f"ruff format exited {exit_code}"

        # --- 3. Read the reformatted file back out. ---
        try:
            bits, _ = container.get_archive(tmp_path)
            tar_bytes = b"".join(bits)
            tar_buf = io.BytesIO(tar_bytes)
            with tarfile.open(fileobj=tar_buf, mode="r") as tar:
                members = tar.getmembers()
                if not members:
                    return code, "ruff format produced no output"
                f = tar.extractfile(members[0])
                formatted = f.read().decode("utf-8") if f else code
        except Exception as exc:
            container.exec_run(["rm", "-f", tmp_path])
            return code, f"failed to read formatted result: {exc}"
        finally:
            container.exec_run(["rm", "-f", tmp_path])

        return formatted, None

    def sync_env(self, environment_id: str) -> str:
        """Run `uv sync` for the env's project.

        Called from the Sync button. Uses plain sync (not --frozen) so
        if the user edited pyproject.toml manually it gets reconciled.
        Returns the combined stdout/stderr.
        """
        container = self._get_running_container(environment_id)
        exit_code, output = container.exec_run(
            ["uv", "sync"],
            workdir="/env/files",
            user="jredux",
            environment={
                "HOME": "/home/jredux",
                "UV_PROJECT_ENVIRONMENT": "/env/.venv",
                "UV_LINK_MODE": "copy",
            },
        )
        return output.decode(errors="replace")

    def pull_repo_stream(
        self, environment_id: str, repo_name: str,
    ):
        """Run git fetch origin + git reset --hard, yielding output chunks."""
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"

        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code != 0:
            raise FileNotFoundError(f"Repo '{repo_name}' not found")

        # All git operations run as jredux so file ownership stays
        # consistent with the original clone.
        git_env = {"HOME": "/home/jredux"}

        # Unshallow if needed (clones use --depth 1)
        exit_code, _ = container.exec_run(
            ["git", "-C", dest, "rev-parse", "--is-shallow-repository"],
            user="jredux",
            environment=git_env,
        )
        is_shallow = (
            exit_code == 0
            and _.decode(errors="replace").strip() == "true"
        )

        if is_shallow:
            _, stream = container.exec_run(
                ["git", "-C", dest, "fetch", "--unshallow", "origin"],
                user="jredux",
                environment=git_env,
                stream=True,
            )
            for chunk in stream:
                yield chunk.decode(errors="replace")

        _, stream = container.exec_run(
            ["git", "-C", dest, "pull", "--ff-only"],
            user="jredux",
            environment=git_env,
            stream=True,
        )
        for chunk in stream:
            yield chunk.decode(errors="replace")

    def list_repos(self, environment_id: str) -> list[dict]:
        """List cloned repos under /env/repos/."""
        container = self._get_running_container(environment_id)
        script = (
            "import os, json\n"
            f"root = '{self.REPOS_ROOT}'\n"
            "os.makedirs(root, exist_ok=True)\n"
            "repos = []\n"
            "for name in sorted(os.listdir(root)):\n"
            "    path = os.path.join(root, name)\n"
            "    if os.path.isdir(path):\n"
            "        markers = []\n"
            "        for m in ['pyproject.toml','setup.py',"
            "'setup.cfg','requirements.txt']:\n"
            "            if os.path.isfile(os.path.join(path, m)):\n"
            "                markers.append(m)\n"
            "        installable = any(m in markers for m in "
            "['pyproject.toml','setup.py','setup.cfg'])\n"
            "        repos.append({'name': name, 'path': path,"
            " 'markers': markers, 'installable': installable})\n"
            "print(json.dumps(repos))\n"
        )
        exit_code, output = container.exec_run(
            ["python", "-c", script],
        )
        if exit_code != 0:
            raise RuntimeError(
                f"list_repos failed: {output.decode(errors='replace')}"
            )
        return json.loads(output.decode())

    def delete_repo(
        self, environment_id: str, repo_name: str,
    ) -> None:
        """Remove a cloned repo."""
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"
        # Safety: only allow deleting under REPOS_ROOT
        if ".." in repo_name or "/" in repo_name:
            raise ValueError("Invalid repo name")
        exit_code, output = container.exec_run(
            ["rm", "-rf", dest],
        )
        if exit_code != 0:
            raise RuntimeError(
                f"delete_repo failed: {output.decode(errors='replace')}"
            )

    def record_activity(self, environment_id: str) -> None:
        self._last_activity[environment_id] = time.time()

    # -----------------------------------------------------------------
    #  Resource stats (CPU / memory / GPU)
    # -----------------------------------------------------------------

    def get_container_stats(self, environment_id: str) -> dict | None:
        """Return {cpu_pct, mem_used, mem_total} for the env's container.

        cpu_pct is normalized to [0, 100] across all cores (so 100 means
        every core is pegged). None if the container isn't running.
        """
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
        except docker.errors.NotFound:
            return None
        if container.status != "running":
            return None

        try:
            stats = container.stats(stream=False)
        except Exception:
            logger.debug("stats() failed for %s", name, exc_info=True)
            return None

        cpu_pct = _compute_cpu_pct(stats)
        mem = stats.get("memory_stats") or {}
        mem_used = int(mem.get("usage") or 0)
        mem_total = int(mem.get("limit") or 0)
        return {
            "cpu_pct": round(cpu_pct, 1),
            "mem_used": mem_used,
            "mem_total": mem_total,
        }

    def get_gpu_stats(self, environment_id: str) -> dict | None:
        """Aggregate GPU stats from nvidia-smi inside the container.

        Returns {util_pct, mem_used, mem_total} aggregated across all
        GPUs visible to the container. `mem_used`/`mem_total` can be
        None on unified-memory devices (e.g. Grace Blackwell / GB10)
        that don't expose a separate VRAM pool — in that case only
        `util_pct` is meaningful. None if nvidia-smi isn't available
        at all (CPU-only container, missing driver).
        """
        name = self.get_container_name(environment_id)
        try:
            container = self.client.containers.get(name)
        except docker.errors.NotFound:
            return None
        if container.status != "running":
            return None

        try:
            result = container.exec_run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                demux=False,
            )
        except Exception:
            return None

        if result.exit_code != 0 or not result.output:
            return None

        text = result.output.decode("utf-8", errors="replace")
        used_mib, total_mib, max_util, mem_supported, saw_any = (
            _parse_nvidia_smi_csv(text)
        )
        if not saw_any:
            return None
        return {
            "util_pct": max_util,
            "mem_used": used_mib * 1024 * 1024 if mem_supported else None,
            "mem_total": total_mib * 1024 * 1024 if mem_supported else None,
        }

    async def start_idle_monitor(self) -> None:
        self._idle_task = asyncio.create_task(self._idle_monitor_loop())

    async def stop_idle_monitor(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass

    async def _idle_monitor_loop(self) -> None:
        timeout_seconds = settings.container_idle_timeout_minutes * 60
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for env_id in list(self._last_activity.keys()):
                last = self._last_activity.get(env_id, now)
                if now - last > timeout_seconds:
                    try:
                        name = self.get_container_name(env_id)
                        container = self.client.containers.get(name)
                        if container.status == "running":
                            logger.info(
                                "Stopping idle container %s (idle for %d min)",
                                name,
                                (now - last) / 60,
                            )
                            container.stop(timeout=10)
                            self._last_activity.pop(env_id, None)
                    except docker.errors.NotFound:
                        self._last_activity.pop(env_id, None)
                    except Exception:
                        logger.warning(
                            "Failed to stop idle container for env %s", env_id
                        )
