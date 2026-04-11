"""Host-side client for the TCP execution server running inside containers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
CONNECT_RETRIES = 5
CONNECT_RETRY_DELAY = 1.0


class ExecutorClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        for attempt in range(CONNECT_RETRIES):
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=CONNECT_TIMEOUT,
                )
                return
            except (ConnectionRefusedError, OSError, TimeoutError) as e:
                if attempt < CONNECT_RETRIES - 1:
                    await asyncio.sleep(CONNECT_RETRY_DELAY)
                else:
                    raise ConnectionError(
                        f"Could not connect to executor at "
                        f"{self.host}:{self.port}"
                        f" after {CONNECT_RETRIES} attempts"
                    ) from e

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _send(self, msg: dict) -> None:
        if self._writer is None:
            raise ConnectionError("Not connected to executor")
        data = json.dumps(msg) + "\n"
        self._writer.write(data.encode("utf-8"))
        await self._writer.drain()

    async def _recv_line(self) -> dict | None:
        if self._reader is None:
            return None
        try:
            line = await self._reader.readline()
            if not line:
                return None
            return json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, ConnectionError):
            return None

    async def execute(
        self, code: str, notebook_id: str = "",
    ) -> AsyncGenerator[dict, None]:
        msg: dict = {"type": "execute", "code": code}
        if notebook_id:
            msg["notebook_id"] = notebook_id
        await self._send(msg)

        while True:
            msg = await self._recv_line()
            if msg is None:
                break
            yield msg
            # Stop streaming after terminal states
            if msg.get("type") == "state" and msg.get("execution_state") in (
                "completed",
                "errored",
            ):
                break

    async def interrupt(self) -> None:
        await self._send({"type": "interrupt"})
