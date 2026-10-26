"""Supervisor de conexión común a todos los exchanges.

Un adapter concreto solo tiene que decir: a qué URL conectarse, qué enviar
nada más abrir el socket, cada cuánto y qué mandar como keepalive, y qué hacer
con cada mensaje. Todo lo demás —reconexión con backoff, registro de cobertura,
latido por minuto, reconexión proactiva antes del límite del exchange, cierre
limpio— vive aquí y se comporta igual en los tres.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Optional

from ..coverage import CoverageLog
from ..writer import RawWriter, utc_ms
from ..wsmin import WSClient, WSClosed, WSError

log = logging.getLogger(__name__)


class Adapter:
    name = "base"
    stream = "liquidation"

    # Cada cuánto mandamos keepalive (0 = el exchange hace ping y nosotros solo
    # respondemos, que es lo que hace wsmin automáticamente).
    keepalive_seconds: float = 0.0
    # Si no llega ningún byte en este tiempo, damos la conexión por muerta.
    idle_timeout_seconds: float = 120.0

    def __init__(self, cfg, exchange_cfg, writer: RawWriter, coverage: CoverageLog):
        self.cfg = cfg
        self.xcfg = exchange_cfg
        self.writer = writer
        self.coverage = coverage
        self._stop = asyncio.Event()
        self._minute_messages = 0
        self._minute_bytes = 0
        self.connected_since: Optional[int] = None
        self.last_message_at: Optional[int] = None
        self.total_messages = 0
        self.reconnects = 0

    # ------------------------------------------------------ a implementar
    async def subscribe_messages(self) -> list:
        """Mensajes a enviar justo después de abrir el socket."""
        return []

    async def keepalive_message(self):
        """Qué mandar como ping de aplicación. None si no hace falta."""
        return None

    def handle(self, message) -> bool:
        """Procesa un mensaje. Devuelve True si es dato que hay que grabar.

        La implementación por defecto graba todo lo que no sea una respuesta de
        control. Cada adapter afina qué es control.
        """
        return True

    # -------------------------------------------------------------- ciclo
    async def run(self) -> None:
        delay = self.cfg.reconnect_min_seconds
        while not self._stop.is_set():
            ws = None
            try:
                log.info("[%s] conectando a %s", self.name, self.xcfg.ws_url)
                ws = await WSClient.connect(self.xcfg.ws_url, open_timeout=20)
                for msg in await self.subscribe_messages():
                    await ws.send(msg if isinstance(msg, str) else json.dumps(msg))
                    await asyncio.sleep(0.12)  # los exchanges limitan subs por segundo
                self.connected_since = utc_ms()
                self.coverage.mark_up(url=self.xcfg.ws_url)
                delay = self.cfg.reconnect_min_seconds
                log.info("[%s] conectado y suscrito", self.name)
                await self._pump(ws)
            except asyncio.CancelledError:
                if ws is not None:
                    await ws.close()
                raise
            except (WSClosed, WSError, OSError, EOFError, asyncio.TimeoutError) as exc:
                self.coverage.mark_down(reason=type(exc).__name__, detail=str(exc)[:300])
                log.warning("[%s] conexión perdida: %s", self.name, exc)
            except Exception as exc:  # nunca dejar morir el supervisor
                self.coverage.mark_down(reason="unexpected", detail=repr(exc)[:300])
                log.exception("[%s] error inesperado", self.name)
            finally:
                self.connected_since = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            if self._stop.is_set():
                break
            self.reconnects += 1
            # backoff exponencial con jitter: sin jitter, tras una caída del
            # exchange todos los clientes del mundo reconectan a la vez.
            wait = min(delay, self.cfg.reconnect_max_seconds) * (0.5 + random.random())
            log.info("[%s] reintentando en %.1fs", self.name, wait)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.cfg.reconnect_max_seconds)

    async def _pump(self, ws: WSClient) -> None:
        opened = time.monotonic()
        last_keepalive = time.monotonic()
        last_beat = time.monotonic()

        while not self._stop.is_set():
            # Reconexión proactiva: mejor cortar nosotros en un momento
            # tranquilo que dejar que el exchange nos corte a mitad de cascada.
            if time.monotonic() - opened > self.cfg.proactive_reconnect_seconds:
                log.info("[%s] reconexión proactiva por antigüedad de la conexión", self.name)
                self.coverage.mark_down(reason="proactive_recycle")
                return

            try:
                raw = await ws.recv(timeout=min(5.0, self.idle_timeout_seconds))
            except asyncio.TimeoutError:
                raw = None
                if self.last_message_at and (utc_ms() - self.last_message_at) / 1000 > self.idle_timeout_seconds:
                    self.coverage.mark_down(reason="idle_timeout")
                    return
            except WSClosed:
                self.coverage.mark_down(reason="server_close")
                return

            now = time.monotonic()

            if raw is not None:
                self.last_message_at = utc_ms()
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                except (ValueError, UnicodeDecodeError):
                    payload = {"_nonjson": raw if isinstance(raw, str) else raw.decode("utf-8", "replace")}
                if self.handle(payload):
                    self.writer.write(payload, received_at=self.last_message_at)
                    self.total_messages += 1
                    self._minute_messages += 1
                    self._minute_bytes += len(raw)

            if self.keepalive_seconds and now - last_keepalive >= self.keepalive_seconds:
                msg = await self.keepalive_message()
                if msg is not None:
                    await ws.send(msg if isinstance(msg, str) else json.dumps(msg))
                last_keepalive = now

            if now - last_beat >= self.cfg.heartbeat_seconds:
                self.coverage.heartbeat(self._minute_messages, self._minute_bytes)
                self._minute_messages = 0
                self._minute_bytes = 0
                last_beat = now

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "exchange": self.name,
            "stream": self.stream,
            "state": self.coverage.state,
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "total_messages": self.total_messages,
            "reconnects": self.reconnects,
            "gaps": self.coverage.gaps,
            "lines_written": self.writer.lines_written,
        }
