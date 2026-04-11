"""Docker container lifecycle management for environments."""

from __future__ import annotations

import asyncio
import logging
import time

import docker
import docker.errors
import docker.types

from src.config import settings
from src.models import ContainerState, ContainerStatus

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
                    "PYTHONPATH": "/env/lib",
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
