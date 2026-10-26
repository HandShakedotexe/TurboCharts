"""Bybit v5 linear: `allLiquidation.{symbol}`.

A diferencia de Binance y OKX, Bybit NO tiene un topic global: hay que
suscribirse símbolo a símbolo. Eso obliga a dos cosas.

1. Enumerar los instrumentos al arrancar vía REST y trocear la suscripción,
   porque Bybit limita los args por mensaje.
2. Refrescar la lista periódicamente: cada listado nuevo que no esté suscrito
   es un símbolo del que no grabamos nada y no nos enteramos.

A cambio, este es el único de los tres feeds que publica TODAS las
liquidaciones. Es, por tanto, la única fuente de verdad disponible para
calibrar el modelo de heatmap.

Keepalive: Bybit exige un {"op":"ping"} de aplicación cada 20 s.
"""
from __future__ import annotations

import logging

from ..rest import get_json
from .base import Adapter

log = logging.getLogger(__name__)

# Bybit acepta varios args por mensaje; troceamos conservador.
ARGS_PER_SUBSCRIBE = 10


class BybitAdapter(Adapter):
    name = "bybit"
    stream = "liquidation"
    keepalive_seconds = 20.0
    idle_timeout_seconds = 90.0   # con el ping cada 20s, 90s sin nada es muerte

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.symbols: list[str] = []

    async def fetch_symbols(self) -> list[str]:
        url = f"{self.xcfg.rest_url}/v5/market/instruments-info?category=linear&limit=1000"
        out: list[str] = []
        cursor = None
        for _ in range(20):  # tope de páginas, por si acaso
            u = url + (f"&cursor={cursor}" if cursor else "")
            data = await get_json(u)
            result = data.get("result", {})
            for item in result.get("list", []):
                if item.get("status") == "Trading" and item.get("symbol"):
                    out.append(item["symbol"])
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return sorted(set(out))

    async def subscribe_messages(self):
        try:
            self.symbols = await self.fetch_symbols()
        except Exception as exc:
            log.warning("[bybit] no se pudo listar instrumentos (%s); uso top_symbols", exc)
            self.symbols = list(self.xcfg.poll_symbols or self.cfg.top_symbols)
        if not self.symbols:
            raise RuntimeError("bybit: sin símbolos que suscribir")
        log.info("[bybit] suscribiendo %d símbolos", len(self.symbols))
        topics = [f"allLiquidation.{s}" for s in self.symbols]
        msgs = []
        for i in range(0, len(topics), ARGS_PER_SUBSCRIBE):
            msgs.append({"op": "subscribe", "args": topics[i : i + ARGS_PER_SUBSCRIBE]})
        return msgs

    async def keepalive_message(self):
        return {"op": "ping"}

    def handle(self, message) -> bool:
        if not isinstance(message, dict):
            return True
        op = message.get("op")
        if op in ("subscribe", "ping", "pong"):
            return False
        if message.get("ret_msg") == "pong":
            return False
        return "topic" in message
