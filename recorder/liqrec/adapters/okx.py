"""OKX v5: canal `liquidation-orders` para instType=SWAP.

Una sola suscripción cubre todos los perpetuos, como en Binance.

Dos particularidades que hay que respetar:

1. `sz` viene en CONTRATOS, no en moneda base. Sin el `ctVal` del instrumento
   no se puede calcular el notional. Por eso el recorder guarda un snapshot
   diario de /api/v5/public/instruments: las especificaciones de contrato
   cambian y tampoco son recuperables hacia atrás.

2. OKX cierra la conexión si no hay tráfico en 30 s. Hay que mandar el string
   literal "ping" (no JSON) y esperar "pong".

Como Binance, está limitado a un update por contrato por segundo.
"""
from __future__ import annotations

from .base import Adapter


class OkxAdapter(Adapter):
    name = "okx"
    stream = "liquidation"
    keepalive_seconds = 20.0      # el límite de OKX es 30 s de inactividad
    idle_timeout_seconds = 90.0

    async def subscribe_messages(self):
        return [{"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}]

    async def keepalive_message(self):
        return "ping"   # texto plano, no JSON: así lo exige OKX

    def handle(self, message) -> bool:
        if isinstance(message, dict):
            if message.get("event") in ("subscribe", "unsubscribe", "error"):
                return message.get("event") == "error"   # los errores sí se graban
            if "_nonjson" in message:
                return message["_nonjson"] != "pong"
            return "data" in message
        return True
