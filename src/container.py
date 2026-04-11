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

    def create_container(
        self,
        environment_id: str,
        python_version: str = "3.11",
        gpu: bool = False,
    ) -> ContainerState:
        name = self.get_container_name(environment_id)
        vol_name = self._volume_name(environment_id)

        try:
            # Create volume if it doesn't exist
            try:
                self.client.volumes.get(vol_name)
            except docker.errors.NotFound:
                self.client.volumes.create(vol_name)

            image = self.get_image_tag(python_version, gpu)

            run_kwargs: dict = {
                "image": image,
                "name": name,
                "detach": True,
                "volumes": {
                    vol_name: {"bind": "/env", "mode": "rw"},
                },
                "environment": {
                    "PYTHONPATH": "/env/lib:/env/files",
                },
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

        # Run git clone
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([url, dest])

        exit_code, output = container.exec_run(cmd)
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
        """Run pip install -e on a cloned repo."""
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"

        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code != 0:
            raise FileNotFoundError(f"Repo '{repo_name}' not found")

        exit_code, output = container.exec_run(
            ["pip", "install", "-e", dest],
        )
        return output.decode(errors="replace")

    def install_repo_stream(
        self, environment_id: str, repo_name: str,
    ):
        """Run pip install -e, yielding output chunks as they arrive."""
        container = self._get_running_container(environment_id)
        dest = f"{self.REPOS_ROOT}/{repo_name}"

        exit_code, _ = container.exec_run(["test", "-d", dest])
        if exit_code != 0:
            raise FileNotFoundError(f"Repo '{repo_name}' not found")

        _, stream = container.exec_run(
            ["pip", "install", "-e", dest],
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
