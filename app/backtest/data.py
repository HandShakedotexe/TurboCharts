"""Acceso a datos point-in-time para el backtester.

Todo loader filtra por `available_at`, nunca por `event_time` -- un
backtest que pregunta "que sabia el mercado en tal instante" y filtra por
`event_time` deja usar informacion que en ese instante todavia no existia
(look-ahead bias, el bug clasico que este proyecto evita en cada capa que
lee datos historicos).

Los loaders devuelven listas de tuplas nombradas simples (no cursores, no
generadores perezosos): `engine.py` necesita poder indexar y hacer bisect
sobre los tiempos repetidamente durante el loop hacia adelante, y estas
ventanas de backtest (dias a un año de barras) caben comodas en memoria --
la misma eleccion que ya hace `api/routers/heatmap.py` con sus listas de
trades y niveles.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

from api.heatmap_model import MmrTable, MmrTier
from db.load_historical import _connect


class Bar(NamedTuple):
    time_ms: int   # event_time (close_time de la vela), epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float


class FundingPoint(NamedTuple):
    time_ms: int
    rate: float


class OiPoint(NamedTuple):
    time_ms: int
    sum_open_interest: Optional[float]
    count_long_short_ratio: Optional[float]
    sum_toptrader_long_short_ratio: Optional[float]
    # Notional en USD (a diferencia de sum_open_interest, en la moneda
    # base) -- opcional con default para no romper las llamadas
    # posicionales existentes (tests, indicators.py). Se agrego para
    # api/routers/indicators.py: graficar OI en USD es comparable entre
    # simbolos, graficarlo en moneda base no.
    sum_open_interest_value: Optional[float] = None


class BigTrade(NamedTuple):
    time_ms: int
    price: float
    notional_usd: float


class RealLiquidation(NamedTuple):
    """Evento de liquidacion REAL (grabador dedicado) -- distinto de los
    `LiquidationLevel` modelados de `api.heatmap_model` (reales y
    proyectadas nunca se mezclan en la misma tabla/tipo). `side`: lado de
    la POSICION liquidada, no de la orden (mismo criterio que
    `liqrec.normalize.Liquidation`, ya verificado contra las tres
    convenciones de exchange)."""
    time_ms: int
    side: str          # "long" | "short"
    price: float
    quote_qty_usd: float


def connect():
    """Misma conexion que ya usa el resto del proyecto (`api/db.py`,
    `db/load_historical.py`) -- un unico DSN, LIQINTEL_DSN o POSTGRES_*."""
    return _connect()


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def load_bars(conn, symbol: str, interval: str, start: datetime, end: datetime,
              exchange: str = "binance") -> list[Bar]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_time, open, high, low, close, volume, taker_buy_volume
               FROM candles
               WHERE exchange=%s AND symbol=%s AND interval=%s
                 AND available_at >= %s AND available_at <= %s
               ORDER BY event_time""",
            (exchange, symbol, interval, start, end),
        )
        rows = cur.fetchall()
    return [
        Bar(_to_ms(t), o, h, l, c, v, tbv if tbv is not None else 0.0)
        for t, o, h, l, c, v, tbv in rows
    ]


def load_funding(conn, symbol: str, start: datetime, end: datetime,
                  exchange: str = "binance") -> list[FundingPoint]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_time, funding_rate FROM funding_rate
               WHERE exchange=%s AND symbol=%s
                 AND available_at >= %s AND available_at <= %s
               ORDER BY event_time""",
            (exchange, symbol, start, end),
        )
        rows = cur.fetchall()
    return [FundingPoint(_to_ms(t), rate) for t, rate in rows]


def load_oi_metrics(conn, symbol: str, start: datetime, end: datetime,
                     exchange: str = "binance") -> list[OiPoint]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_time, sum_open_interest, count_long_short_ratio,
                      sum_toptrader_long_short_ratio, sum_open_interest_value
               FROM open_interest_metrics
               WHERE exchange=%s AND symbol=%s
                 AND available_at >= %s AND available_at <= %s
               ORDER BY event_time""",
            (exchange, symbol, start, end),
        )
        rows = cur.fetchall()
    return [OiPoint(_to_ms(t), oi, cr, tr, val) for t, oi, cr, tr, val in rows]


def load_big_trades(conn, symbol: str, start: datetime, end: datetime,
                     exchange: str = "binance") -> list[BigTrade]:
    """Mismo criterio de disponibilidad que `_historical_big_trades` en
    api/routers/heatmap.py: `available_at == event_time` para esta tabla
    (un trade impreso es publico al instante, ver schema.sql)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_time, price, notional_usd FROM big_trades
               WHERE exchange=%s AND symbol=%s
                 AND available_at >= %s AND available_at <= %s
               ORDER BY event_time""",
            (exchange, symbol, start, end),
        )
        rows = cur.fetchall()
    return [BigTrade(_to_ms(t), price, notional) for t, price, notional in rows]


def load_bars_live(symbol: str, interval: str, start: datetime, end: datetime) -> list[Bar]:
    """UNICA funcion de este modulo que toca la red en vez de la DB --
    documentado a proposito, no una costumbre nueva (el resto de
    `backtest/` es deliberadamente solo-DB). Hace falta porque `candles`
    es un volcado MENSUAL que no publica el mes en curso (mismo hueco que
    ya resuelve `api/routers/live.py` para el chart en vivo), y el
    grabador de liquidaciones REALES puede tener historia dentro de ese
    hueco. Sin esto, `strategies/liquidation_bounce.py` (el UNICO trigger
    con historia en ese rango) no tiene ninguna vela contra la cual
    backtestear.

    Misma paginacion por `startTime` que `_fetch_since` en
    api/routers/live.py (Binance limita a 1500 velas por llamada); no se
    reusa esa funcion tal cual porque devuelve `time` en SEGUNDOS (para el
    chart) mientras `Bar.time_ms` necesita milisegundos."""
    import json
    import urllib.request

    start_ms, end_ms = _to_ms(start), _to_ms(end)
    out: list[Bar] = []
    cursor = start_ms
    for _ in range(60):   # 60 * 1500 velas de 5m son > 6 meses -- margen de sobra
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&limit=1500")
        req = urllib.request.Request(url, headers={"User-Agent": "liq-intel-backtest/0.1"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            page = json.loads(resp.read())
        if not page:
            break
        for row in page:
            close_ms = int(row[6])
            if close_ms > end_ms:
                break
            out.append(Bar(close_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                            float(row[5]), float(row[9])))
        last_open_ms = int(page[-1][0])
        if len(page) < 1500 or last_open_ms >= end_ms:
            break
        cursor = last_open_ms + 1
    return out


def load_real_liquidations(
    symbol: str, start: datetime, end: datetime, data_dir: Optional[Path] = None,
) -> list[RealLiquidation]:
    """Liquidaciones REALES para el backtester -- unica fuente de verdad
    para el trigger de `strategies/liquidation_bounce.py`. NO lee la DB
    (no hay tabla, ver el docstring de api/real_liquidations.py): reusa
    `fetch_liquidations_since` TAL CUAL -- mismo motivo que ese modulo ya
    reusa `liqrec.normalize.from_binance`, evitar una segunda
    implementacion del mapeo de lados (uno de los puntos mas faciles de
    equivocar de todo el proyecto).

    Point-in-time: `available_at == event_time`, mismo criterio que
    `load_big_trades` (un evento de liquidacion impreso por el exchange es
    publico al instante) -- pero esto no se filtra por `available_at` como
    los loaders de la DB porque `fetch_liquidations_since` ya devuelve
    exactamente los eventos con `event_time >= start`; el filtro superior
    (`<= end`) se aplica aqui porque esa funcion esta pensada para "desde
    `start` hasta AHORA" (uso en vivo), no para una ventana cerrada como
    necesita un backtest historico."""
    # Import perezoso: mismo motivo que `_connect` en db/load_historical.py
    # importa psycopg perezosamente -- evita que todo `backtest/` dependa de
    # que `api/` (FastAPI y demas) este instalado solo para poder importar
    # `data.py`, cuando la mayoria de los loaders de aqui ni lo tocan.
    from api.real_liquidations import fetch_liquidations_since

    events = fetch_liquidations_since(symbol, start, limit=1_000_000, data_dir=data_dir)
    end_ms = _to_ms(end)
    out = [
        RealLiquidation(e.event_time, e.side, e.price, e.quote_qty)
        for e in events if e.event_time <= end_ms
    ]
    out.sort(key=lambda r: r.time_ms)
    return out


def load_mmr_table(conn, symbol: str, as_of: datetime, exchange: str = "binance") -> MmrTable:
    """Identico a `_mmr_table` en api/routers/heatmap.py -- el tramo de MMR
    vigente en `as_of` (`margin_brackets` es un snapshot de reglas, no una
    serie de mercado, ver su comentario en schema.sql)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT notional_floor, notional_cap, maintenance_margin_rate, max_leverage
               FROM margin_brackets
               WHERE exchange=%s AND symbol=%s AND fetched_at = (
                   SELECT max(fetched_at) FROM margin_brackets
                   WHERE exchange=%s AND symbol=%s AND fetched_at <= %s
               )
               ORDER BY bracket_seq""",
            (exchange, symbol, exchange, symbol, as_of),
        )
        rows = cur.fetchall()
    return MmrTable([MmrTier(*r) for r in rows])
