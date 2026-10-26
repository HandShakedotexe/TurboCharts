"""Binance USD-M: `!forceOrder@arr`.

Una sola suscripción cubre todos los símbolos, así que no hay que enumerar nada.

ENDPOINT: tiene que ser el de `/market`.

    wss://fstream.binance.com/market/stream?streams=!forceOrder@arr

Binance dividió sus WebSockets en /public, /market y /private, y desmanteló las
URLs antiguas el 2026-04-23. Las liquidaciones son dato de "market". Si se usa
la URL antigua, el handshake funciona, la conexión queda abierta, el keepalive
responde... y no llega jamás un solo mensaje. No hay error, no hay cierre, no
hay nada que un test de conexión pueda detectar. Solo silencio que parece un
mercado tranquilo.

Lo que hay que recordar de este feed: solo llega la ÚLTIMA orden de liquidación
por símbolo en cada ventana de 1000 ms. En una cascada, lo que graba este
adapter es una muestra, no el total. El campo is_throttled de la normalización
lo marca y ninguna agregación multi-exchange debe ignorarlo.

Keepalive: el servidor manda ping cada pocos minutos y wsmin responde pong solo.
Binance cierra las conexiones a las 24 h; el supervisor recicla antes.
"""
from __future__ import annotations

from .base import Adapter


class BinanceAdapter(Adapter):
    name = "binance"
    stream = "liquidation"
    keepalive_seconds = 0.0        # el servidor hace ping; wsmin responde
    idle_timeout_seconds = 600.0   # puede haber minutos sin liquidaciones

    async def subscribe_messages(self):
        # La URL combinada ya lleva ?streams=!forceOrder@arr, no hace falta
        # enviar un SUBSCRIBE. Si alguien configura la URL "cruda" (/ws), se
        # envía la suscripción explícita.
        if "streams=" in self.xcfg.ws_url:
            return []
        return [{"method": "SUBSCRIBE", "params": ["!forceOrder@arr"], "id": 1}]

    def handle(self, message) -> bool:
        # Respuestas de control del endpoint combinado: {"result":null,"id":1}
        if isinstance(message, dict) and "result" in message and "id" in message:
            return False
        return True
