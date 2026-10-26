"""GET /api/big-trades -- trades grandes (>= bigtrades.BIG_TRADE_THRESHOLD_USD)
recientes, combinando el archivo historico (tabla `big_trades`, ver
db/schema.sql) con la cola en vivo (api/bigtrades.get_recent_big_trades_cached).
Mismo patron que /api/real-liquidations: DB para atras en el tiempo + REST en
vivo para lo mas reciente, sin duplicar la ventana que cubre cada fuente (el
corte es `live_cutoff = ahora - bigtrades.LOOKBACK_HOURS`, la DB solo cubre lo
anterior a eso).

Ventana por defecto acotada a unas pocas horas (no los 90 dias de
`/api/candles`/`/api/real-liquidations`): a diferencia de las liquidaciones
reales (~10-14 dias de historia grabada en total, unos pocos miles de eventos
EN TOTAL, ver LiquidationVolumePane.tsx), `big_trades` tiene 3 meses de
backfill CONTINUO para BTCUSDT a ~3,450 trades/dia (ver api/bigtrades.py,
BIG_TRADE_THRESHOLD_USD) -- pedir 90 dias serian ~310,000 filas por peticion.
Este es un panel de contexto RECIENTE (el tape de trades grandes), no un
historico completo -- LOOKBACK_HOURS_DEFAULT alcanza de sobra para eso.

`side` (2026-09-15, pedido del usuario: burbujas rojo/verde por lado en
`BigTradesOverlay.tsx`) viene del campo "m" de Binance (aggTrades,
"Was the buyer the maker?"): `True` implica que el VENDEDOR fue el agresor
(taker sell -> "sell"), `False` que lo fue el comprador (taker buy ->
"buy"). `null` cuando no se sabe -- filas de `big_trades` ingeridas ANTES
de que existiera la columna `is_buyer_maker` (ver db/schema.sql) no tienen
este dato y no hay forma barata de recuperarlo sin re-descargar esos CSV;
nunca se inventa un lado para esas filas.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query

from .. import bigtrades
from ..db import get_conn

router = APIRouter()

LOOKBACK_HOURS_DEFAULT = 48.0
# Subido de 5000 a 20000 (2026-09-18): con el nuevo slider de "Dias" del
# frontend (App.tsx, 1-7 dias) y `min_notional_usd` filtrando ANTES del
# recorte por limite (ver abajo), una ventana de varios dias con un umbral
# alto sigue devolviendo pocos miles de filas -- pero sin subir el techo,
# `combine_trades` (recorta a los `limit` MAS RECIENTES) podia descartar
# trades grandes de mas atras en la ventana con solo el umbral fijo del
# backend (BIG_TRADE_THRESHOLD_USD, $100k) aplicado, que en 7 dias de
# BTCUSDT ya son ~24,000 trades por si solos.
MAX_LIMIT = 20000


@router.get("/big-trades")
def get_big_trades(
    symbol: str = Query(..., description="p.ej. BTCUSDT"),
    start: Optional[datetime] = Query(
        None, description="ISO 8601. Por defecto, las ultimas 48h."
    ),
    limit: int = Query(3000, le=MAX_LIMIT),
    min_notional_usd: Optional[float] = Query(
        None,
        description=(
            "Filtra por este notional en vez del umbral fijo del backend "
            "(bigtrades.BIG_TRADE_THRESHOLD_USD) -- pedido por el usuario "
            "(2026-09-18) para que el slider de umbral del frontend "
            "filtre ANTES del recorte por `limit`, no solo al dibujar, y "
            "una ventana de varios dias no descarte trades grandes de mas "
            "atras por quedar detras de miles de trades mas chicos y mas "
            "recientes."
        ),
    ),
) -> dict:
    now = datetime.now(timezone.utc)
    if start is None:
        start = now - timedelta(hours=LOOKBACK_HOURS_DEFAULT)
    elif start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    effective_threshold = max(bigtrades.BIG_TRADE_THRESHOLD_USD, min_notional_usd or 0.0)

    live_cutoff = now - timedelta(hours=bigtrades.LOOKBACK_HOURS)
    historical: list[tuple[float, float, int, Optional[bool]]] = []
    if start < live_cutoff:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT price, notional_usd, event_time, is_buyer_maker FROM big_trades
                   WHERE exchange='binance' AND symbol=%s
                     AND available_at >= %s AND available_at < %s
                     AND notional_usd >= %s
                   ORDER BY event_time""",
                (symbol, start, live_cutoff, effective_threshold),
            )
            historical = [
                (price, notional, int(event_time.timestamp() * 1000), is_buyer_maker)
                for price, notional, event_time, is_buyer_maker in cur.fetchall()
            ]

    live_all = bigtrades.get_recent_big_trades_with_side_cached(symbol)
    live = [t for t in live_all if t[1] >= effective_threshold]
    start_ms = int(start.timestamp() * 1000)
    combined = bigtrades.combine_trades(historical, live, start_ms, limit)

    def _side(is_buyer_maker: Optional[bool]) -> Optional[str]:
        if is_buyer_maker is None:
            return None
        return "sell" if is_buyer_maker else "buy"

    return {
        "symbol": symbol.upper(),
        "threshold_usd": bigtrades.BIG_TRADE_THRESHOLD_USD,
        "events": [
            {"event_time_ms": t, "price": p, "notional_usd": n, "side": _side(m)}
            for p, n, t, m in combined
        ],
    }
