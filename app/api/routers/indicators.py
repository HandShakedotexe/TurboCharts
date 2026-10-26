"""GET /api/indicators/{oi,cvd,funding} -- series para graficar en el
chart. Historico archivado (Fase 1, TimescaleDB) + cola en vivo directo a
Binance, mismo patron que /api/candles + /api/live-klines.

CVD reusa `backtest.indicators.cvd` tal cual -- el MISMO calculo que ya
usa el backtester para su señal, ahora tambien visible en el chart. OI y
funding reusan los loaders de `backtest/data.py` (`load_oi_metrics`,
`load_funding`) en vez de repetir las queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from backtest.data import load_bars, load_funding, load_oi_metrics
from backtest.indicators import cvd as compute_cvd

from ..db import get_conn
from .live import (
    _fetch_since,
    _parse_rows,
    fetch_funding_history_since,
    fetch_oi_history_since,
)

router = APIRouter()

DEFAULT_START = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _parse_dt(s: Optional[datetime]) -> datetime:
    if s is None:
        return DEFAULT_START
    return s if s.tzinfo else s.replace(tzinfo=timezone.utc)


def _merge_by_time(historical: list[dict], live: list[dict]) -> list[dict]:
    """Igual que `mergeCandles` en App.tsx: en vivo pisa lo archivado si
    coinciden en tiempo, e implicitamente pone todo en orden."""
    by_time: dict[int, dict] = {p["time"]: p for p in historical}
    for p in live:
        by_time[p["time"]] = p
    return [by_time[t] for t in sorted(by_time)]


# Candle "time" (ver live.py `_parse_rows`) es SIEMPRE `closeTime // 1000`,
# y `closeTime` de Binance es `openTime + duracion - 1ms` -- por construccion,
# cae 1 segundo ANTES de la marca redonda (p.ej. una vela de 5m cierra en
# `...:04:59`, no `...:05:00`). OI y funding, en cambio, vienen de Binance ya
# alineados a la marca redonda (`...:05:00`). Verificado contra datos reales
# de BTCUSDT (2026-09-03): 0 de 629,107 puntos de OI y 0 de 6,580 de funding
# coinciden con ningun `time` de vela, mientras que CVD (que ya reutiliza el
# mismo calculo que las velas) coincide en 622,080/622,080. Ese desfase de 1s
# le inyecta a la escala de tiempo COMPARTIDA del chart (OI/funding viven en
# su propio pane, pero comparten el eje horizontal con el precio) un indice
# logico nuevo por cada punto -- y como la cadencia nativa de OI cambia (5 min
# en `open_interest_metrics` archivado, 1h en la cola en vivo de Binance, ver
# `fetch_oi_history_since`), la DENSIDAD de esos indices extra no es uniforme
# en el tiempo. HeatmapOverlay.tsx interpola precio/tiempo -> pixel de forma
# LINEAL asumiendo espaciado uniforme entre indices (`computeMapping`, unico
# metodo posible: `timeToCoordinate` exige coincidencia EXACTA con un punto ya
# en la escala -- verificado leyendo el propio bundle de lightweight-charts,
# `timeToCoordinate` llama a `timeToIndex(t, false)` -- no interpola). Con
# densidad no uniforme esa interpolacion queda mal en regiones enteras: es la
# causa real de los bloques negros reportados por el usuario al activar Open
# Interest (2026-09-03), no un evento de la libreria que faltara escuchar.
# Restar 1 segundo alinea
# cada punto de OI/funding con el `time` de una vela real que ya existe en la
# escala -- SI esa vela existe con esa cadencia exacta. Verificado que esto
# no bastaba: a 5m (donde OI archivado tambien es de 5 min) quedo perfecto,
# pero a 1h el usuario siguio viendo bloques negros (2026-09-03, misma
# sesion) -- 11 de cada 12 puntos de OI archivado (5 min) no coinciden con
# ninguna vela de 1h. El arreglo de fondo es `_resample_to_bar_grid` mas
# abajo, que reasigna CADA punto al `time` exacto de la vela de la
# temporalidad activa sea cual sea -- esto (el -1s) sigue haciendo falta
# aparte: sin el, un punto de OI en la marca redonda "13:05:00" no cumple
# `punto <= vela` contra una vela de 5m que cierra en "13:04:59", y su
# valor se atribuye a la vela SIGUIENTE en vez de a la que en realidad
# cubre ese instante -- un desfase de una vela, no de escala.
def _align_to_candle_grid(time_seconds: int) -> int:
    return time_seconds - 1


def _bar_time_grid(symbol: str, interval: str, start_dt: datetime, end_dt: datetime) -> list[int]:
    """Los mismos `time` (epoch segundos) que va a usar la serie de precio
    para esta temporalidad -- archivado (Fase 1) + cola en vivo, mismo
    patron que ya usa get_cvd_history. Sirve de grilla para
    `_resample_to_bar_grid`: reasignar OI/funding a ESTOS instantes exactos
    es lo unico que garantiza cero indices logicos nuevos en la escala de
    tiempo compartida del chart, sea cual sea la temporalidad activa (ver
    docstring de _align_to_candle_grid mas abajo -- ese arreglo solo
    cubria el caso en que la cadencia nativa de OI/funding coincidia con
    la temporalidad vista; a 1h, con OI archivado de 5 min, seguia
    rompiendose -- reportado por el usuario 2026-09-03)."""
    with get_conn() as conn:
        bars = load_bars(conn, symbol, interval, start_dt, end_dt)
    since_ms = (bars[-1].time_ms + 1) if bars else int(start_dt.timestamp() * 1000)
    live_raw = _parse_rows(_fetch_since(symbol, interval, since_ms))
    return [b.time_ms // 1000 for b in bars] + [row["time"] for row in live_raw]


def _resample_to_bar_grid(points: list[dict], bar_times: list[int]) -> list[dict]:
    """Reasigna cada punto de una serie (YA ordenada por tiempo) al `time`
    de vela mas reciente <= ese punto -- "ultimo valor conocido"
    (forward-fill), el mismo criterio de resampleo que ya usaba
    heatmap_model antes de la version de intervalos exactos. No inventa
    un punto nuevo: reusa el ultimo
    dato real conocido en el instante EXACTO de cada vela, que es lo unico
    que la escala de tiempo del chart ya tiene como indice."""
    if not points or not bar_times:
        return []
    out = []
    j = 0
    last_value = None
    for t in bar_times:
        while j < len(points) and points[j]["time"] <= t:
            last_value = points[j]["value"]
            j += 1
        if last_value is not None:
            out.append({"time": t, "value": last_value})
    return out


@router.get("/indicators/oi")
def get_oi_history(
    symbol: str = Query(...),
    interval: str = Query("1h"),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
) -> list[dict]:
    start_dt = _parse_dt(start)
    end_dt = end.replace(tzinfo=end.tzinfo or timezone.utc) if end else datetime.now(timezone.utc)
    with get_conn() as conn:
        rows = load_oi_metrics(conn, symbol, start_dt, end_dt)
    historical = [
        {"time": _align_to_candle_grid(r.time_ms // 1000), "value": r.sum_open_interest_value}
        for r in rows if r.sum_open_interest_value is not None
    ]
    since_ms = (historical[-1]["time"] * 1000 + 1) if historical else int(start_dt.timestamp() * 1000)
    live = [
        {"time": _align_to_candle_grid(ts // 1000), "value": val}
        for ts, val in fetch_oi_history_since(symbol, since_ms)
    ]
    merged = _merge_by_time(historical, live)
    bar_times = _bar_time_grid(symbol, interval, start_dt, end_dt)
    return _resample_to_bar_grid(merged, bar_times)


@router.get("/indicators/funding")
def get_funding_history(
    symbol: str = Query(...),
    interval: str = Query("1h"),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
) -> list[dict]:
    start_dt = _parse_dt(start)
    end_dt = end.replace(tzinfo=end.tzinfo or timezone.utc) if end else datetime.now(timezone.utc)
    with get_conn() as conn:
        rows = load_funding(conn, symbol, start_dt, end_dt)
    historical = [{"time": _align_to_candle_grid(r.time_ms // 1000), "value": r.rate} for r in rows]
    since_ms = (historical[-1]["time"] * 1000 + 1) if historical else int(start_dt.timestamp() * 1000)
    live = [
        {"time": _align_to_candle_grid(ts // 1000), "value": rate}
        for ts, rate in fetch_funding_history_since(symbol, since_ms)
    ]
    merged = _merge_by_time(historical, live)
    bar_times = _bar_time_grid(symbol, interval, start_dt, end_dt)
    return _resample_to_bar_grid(merged, bar_times)


@router.get("/indicators/cvd")
def get_cvd_history(
    symbol: str = Query(...),
    interval: str = Query("1h"),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
) -> list[dict]:
    """CVD es ACUMULATIVO -- la cola en vivo no puede recalcularse sola,
    tiene que seguir sumando desde donde termino el CVD historico (no
    desde 0), o la serie mostraria un salto falso en el punto de union."""
    start_dt = _parse_dt(start)
    end_dt = end.replace(tzinfo=end.tzinfo or timezone.utc) if end else datetime.now(timezone.utc)
    with get_conn() as conn:
        bars = load_bars(conn, symbol, interval, start_dt, end_dt)
    running = compute_cvd(bars)
    historical = [{"time": b.time_ms // 1000, "value": v} for b, v in zip(bars, running)]

    since_ms = (bars[-1].time_ms + 1) if bars else int(start_dt.timestamp() * 1000)
    live_raw = _parse_rows(_fetch_since(symbol, interval, since_ms))
    last_running = running[-1] if running else 0.0
    live: list[dict] = []
    for row in live_raw:
        taker_sell = row["volume"] - row["taker_buy_volume"]
        last_running += row["taker_buy_volume"] - taker_sell
        live.append({"time": row["time"], "value": last_running})

    return _merge_by_time(historical, live)
