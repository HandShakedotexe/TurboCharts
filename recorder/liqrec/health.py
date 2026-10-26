"""Estado del recorder: fichero JSON + endpoint HTTP opcional.

`data/health.json` se reescribe de forma atómica cada pocos segundos. El
endpoint HTTP devuelve lo mismo y responde 503 si algún feed lleva demasiado
tiempo caído, para que un uptime monitor externo pueda avisarte sin que tengas
que montar Prometheus el primer día.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class Health:
    def __init__(self, path: Path, snapshot: Callable[[], dict], interval: float = 10.0):
        self.path = Path(path)
        self.snapshot = snapshot
        self.interval = interval
        self._stop = asyncio.Event()

    def write_once(self) -> dict:
        data = self.snapshot()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        return data

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.write_once()
            except Exception:
                log.exception("no se pudo escribir health.json")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


async def serve_health(host: str, port: int, snapshot: Callable[[], dict]):
    """Servidor HTTP mínimo: GET /health -> 200 si todo arriba, 503 si no."""

    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            writer.close()
            return
        data = snapshot()
        healthy = all(f.get("state") == "up" for f in data.get("feeds", [])) if data.get("feeds") else False
        status = "200 OK" if healthy else "503 Service Unavailable"
        body = json.dumps(data, indent=2).encode()
        head = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(head + body)
        try:
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("health HTTP en http://%s:%d/health", host, port)
    return server
