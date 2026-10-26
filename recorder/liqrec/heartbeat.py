"""Interruptor de hombre muerto (dead man's switch).

El riesgo de un grabador no es que se caiga: es que se caiga **en silencio**.
Un hueco de tres días que descubres el jueves es peor que no tener grabador,
porque hasta el jueves creías que tenías los datos.

Este módulo invierte la lógica de la alerta. En vez de avisar cuando algo va
mal —lo que exige que el proceso siga vivo para poder avisar, justo lo que no
podemos suponer— manda una señal periódica MIENTRAS todo va bien. Es el
silencio lo que dispara la alarma, y el silencio no depende de que quede nada
en pie: ni el proceso, ni la máquina, ni la luz, ni el internet.

Funciona con cualquier servicio de tipo cron-monitor que te dé una URL de ping
(healthchecks.io, Better Stack, Cronitor, UptimeRobot heartbeat...). Todos
tienen plan gratuito suficiente para esto.

Si `heartbeat_url` está vacío, el módulo no hace nada.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Callable, Optional

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        url: str,
        snapshot: Callable[[], dict],
        *,
        interval: float = 300.0,
        require_all_feeds: bool = True,
        timeout: float = 10.0,
    ):
        self.url = (url or "").strip()
        self.snapshot = snapshot
        self.interval = interval
        self.require_all_feeds = require_all_feeds
        self.timeout = timeout
        self._stop = asyncio.Event()

        self.sent = 0
        self.skipped = 0
        self.failed = 0
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # ------------------------------------------------------------------ salud
    def is_healthy(self, data: dict) -> bool:
        """Sano = hay feeds y están arriba.

        Los pollers REST no cuentan: que la API de open interest devuelva 429
        un rato no es motivo para gritar, porque ese dato sí se puede rellenar
        después. Los feeds de liquidación no.
        """
        feeds = data.get("feeds") or []
        if not feeds:
            return False
        up = [f for f in feeds if f.get("state") == "up"]
        return len(up) == len(feeds) if self.require_all_feeds else bool(up)

    # ------------------------------------------------------------------- ping
    def _post_sync(self, body: bytes) -> None:
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "liq-intel-recorder/0.1")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()

    async def ping(self, data: dict) -> bool:
        body = json.dumps({
            "uptime_seconds": data.get("uptime_seconds"),
            "feeds": [{k: f.get(k) for k in ("exchange", "state", "total_messages", "reconnects", "gaps")}
                      for f in data.get("feeds", [])],
        }, separators=(",", ":")).encode()
        try:
            await asyncio.to_thread(self._post_sync, body)
            self.sent += 1
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Que falle el ping NUNCA puede tumbar el grabador: si no hay
            # internet, lo que importa es seguir escribiendo en disco, y el
            # silencio ya está haciendo su trabajo del otro lado.
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("heartbeat: no se pudo enviar el ping (%s)", self.last_error)
            return False

    async def run(self) -> None:
        if not self.enabled:
            log.info("heartbeat desactivado (sin heartbeat_url): una caída pasará inadvertida")
            return
        log.info("heartbeat cada %.0fs hacia %s", self.interval, _redact(self.url))
        while not self._stop.is_set():
            try:
                data = self.snapshot()
                if self.is_healthy(data):
                    await self.ping(data)
                else:
                    # Deliberadamente NO se avisa del fallo: se deja de pingar.
                    # El monitor externo salta solo cuando pasa el plazo.
                    self.skipped += 1
                    log.debug("heartbeat: estado no sano, no se envía ping")
            except Exception:
                log.exception("heartbeat: error inesperado")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_error": self.last_error,
        }


def _redact(url: str) -> str:
    """No escupir el token del ping en los logs."""
    if len(url) <= 28:
        return url
    return url[:22] + "…" + url[-4:]
