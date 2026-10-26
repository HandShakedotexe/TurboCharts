"""GET /api/candles y /api/symbols -- velas OHLCV reales desde TimescaleDB.

Sin transformar ni modelar nada: esto es dato observado, a diferencia de
/api/heatmap. Lightweight Charts espera `time` en segundos unix (UTCTimestamp),
por eso la conversion aqui.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from ..db import get_conn

router = APIRouter()

_INTERVAL_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def _interval_minutes(interval: str) -> int:
    """Convierte "5m"/"1h"/"4h"/"1d" a minutos para poder ordenar
    cronologicamente -- un `ORDER BY interval` de SQL los deja alfabeticos
    ("1h" antes que "4h" antes que "5m", reportado por el usuario
    2026-09-29 con una captura del dropdown desordenado). Por unidad+numero
    en vez de una lista fija de intervalos conocidos, para que cualquier
    temporalidad nueva que se agregue despues (15m, 1d, ...) tambien caiga
    en el lugar correcto sin tocar este archivo."""
    unit = interval[-1]
    return int(interval[:-1]) * _INTERVAL_UNIT_MINUTES[unit]


@router.get("/symbols")
def list_symbols() -> list[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM candles WHERE exchange='binance' ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


@router.get("/intervals")
def list_intervals(symbol: str = Query(...)) -> list[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT interval FROM candles WHERE exchange='binance' AND symbol=%s",
            (symbol,),
        )
        return sorted((r[0] for r in cur.fetchall()), key=_interval_minutes)


@router.get("/candles")
def get_candles(
    symbol: str = Query(..., description="p.ej. BTCUSDT"),
    interval: str = Query("1h"),
    start: Optional[datetime] = Query(None, description="ISO 8601, inclusive"),
    end: Optional[datetime] = Query(None, description="ISO 8601, inclusive"),
    limit: int = Query(1_000_000, le=1_000_000),
) -> list[dict]:
    """Sin `start`/`end`, el limite por defecto (antes 5000) recortaba la
    historia a los primeros meses archivados -- el chart se veia "completo"
    solo porque el hueco frente al tramo en vivo se comprime visualmente en
    el eje de tiempo. Con 6 años de velas de 5 minutos (~630k filas) hace
    falta un techo mucho mas alto.

    Si algun dia SI hiciera falta recortar, hay que quedarse con lo mas
    RECIENTE, no con lo mas viejo -- de ahi el DESC + LIMIT + reversed()
    en vez de ASC + LIMIT."""
    where = ["exchange = 'binance'", "symbol = %(symbol)s", "interval = %(interval)s"]
    params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
    if start:
        where.append("event_time >= %(start)s")
        params["start"] = start
    if end:
        where.append("event_time <= %(end)s")
        params["end"] = end

    sql = f"""
        SELECT event_time, open, high, low, close, volume, taker_buy_volume
        FROM candles
        WHERE {' AND '.join(where)}
        ORDER BY event_time DESC
        LIMIT %(limit)s
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {"time": int(r[0].timestamp()), "open": r[1], "high": r[2], "low": r[3],
         "close": r[4], "volume": r[5], "taker_buy_volume": r[6]}
        for r in reversed(rows)
    ]
