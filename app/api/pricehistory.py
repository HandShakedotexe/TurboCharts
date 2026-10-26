"""Maximos/minimos recientes de precio, directo a Binance -- sirve para
decidir si una liquidacion modelada ya fue "comida" por el precio (ver
`heatmap_model.filter_consumed_levels`). NO persiste en TimescaleDB, igual
que `bigtrades.py`: es una foto reciente, no un archivo historico.

Una sola llamada sin paginar basta: `LOOKBACK_HOURS` horas de velas de 1
minuto caben de sobra en el limite de 1500 velas por peticion de Binance
(12h = 720 velas), asi que no hace falta el patron de buffer incremental de
`bigtrades.py` -- ese existe porque los aggTrades de un simbolo activo
superan las decenas de miles en la misma ventana.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .routers.live import BINANCE_REST, _fetch_since, _get_json

INTERVAL = "1m"
# Mismo horizonte que bigtrades.LOOKBACK_HOURS: no tiene sentido evaluar
# decaimiento contra precio mas viejo que los trades que generan los niveles.
LOOKBACK_HOURS = 12.0
# Igual al HEATMAP_POLL_MS del frontend -- no hace falta pedir esto mas
# seguido de lo que el heatmap se vuelve a pintar.
CACHE_TTL_SECONDS = 20.0

# Tramo extendido, para el decaimiento de niveles construidos con big trades
# HISTORICOS (tabla `big_trades`) que van mas atras que las 12h que cubre
# LOOKBACK_HOURS. 5m en vez de 1m: la tabla `candles` de la Fase 1 todavia no
# llega a este mes, asi que este tramo se pide en vivo a Binance igual que el
# reciente -- 7 dias de 1m serian 10,080 velas y varias paginas; de 5m son
# 2,016, dos paginas de sobra dentro de MAX_PAGES de `_fetch_since`.
EXTENDED_INTERVAL = "5m"
# Subido de 7 a 90 dias (2026-08-31) para que coincida con
# heatmap.HISTORICAL_LOOKBACK_DAYS -- si esta ventana es mas corta que la
# de los big trades historicos, `compute_liquidity_grid`/
# `filter_consumed_levels` no tienen con que evaluar el decaimiento de
# los niveles mas viejos (su docstring exige que ninguna barra sea mas
# ancha que el hueco entre el inicio de `price_history` y el trade que
# origino un nivel). Medido (no adivinado): 90 dias de velas de 5m via
# `_fetch_since` tardan ~6.6s -- aceptable, se piden con
# EXTENDED_CACHE_TTL_SECONDS de por medio (5 min), no en cada refresco
# del heatmap (20s).
EXTENDED_LOOKBACK_DAYS = 90.0
# Mas largo que CACHE_TTL_SECONDS: el tramo antiguo no cambia con cada vela
# nueva como el reciente, y son 2 paginas por refresco en vez de 1 -- no
# tiene sentido pedirlo tan seguido.
EXTENDED_CACHE_TTL_SECONDS = 300.0


def _fetch_high_low(symbol: str, lookback_hours: float) -> list[tuple[int, float, float]]:
    limit = min(1500, int(lookback_hours * 60) + 5)
    url = f"{BINANCE_REST}/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={limit}"
    raw = _get_json(url)
    # closeTime (indice 6), high (2), low (3) -- mismo orden de columnas que
    # ya usa live.py._parse_rows.
    return [(int(row[6]), float(row[2]), float(row[3])) for row in raw]


@dataclass
class _Cache:
    data: list[tuple[int, float, float]] = field(default_factory=list)
    fetched_at: float = 0.0


_cache: dict[str, _Cache] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(symbol: str) -> threading.Lock:
    with _locks_guard:
        if symbol not in _locks:
            _locks[symbol] = threading.Lock()
        return _locks[symbol]


def get_recent_high_low_cached(
    symbol: str, lookback_hours: float = LOOKBACK_HOURS
) -> list[tuple[int, float, float]]:
    """[(close_time_ms, high, low), ...] ordenado por tiempo ascendente,
    cacheado `CACHE_TTL_SECONDS` por simbolo (candado por simbolo, mismo
    patron que `bigtrades._lock_for` -- evita pedir lo mismo dos veces si el
    heatmap se refresca en paralelo para el mismo simbolo)."""
    now = time.monotonic()
    entry = _cache.get(symbol)
    if entry and now - entry.fetched_at < CACHE_TTL_SECONDS:
        return entry.data

    with _lock_for(symbol):
        now = time.monotonic()
        entry = _cache.get(symbol)
        if entry and now - entry.fetched_at < CACHE_TTL_SECONDS:
            return entry.data
        data = _fetch_high_low(symbol, lookback_hours)
        _cache[symbol] = _Cache(data=data, fetched_at=time.monotonic())
        return data


def _fetch_extended_high_low(symbol: str, lookback_days: float) -> list[tuple[int, float, float]]:
    start_ms = int((time.time() - lookback_days * 86400) * 1000)
    raw = _fetch_since(symbol, EXTENDED_INTERVAL, start_ms)
    return [(int(row[6]), float(row[2]), float(row[3])) for row in raw]


_extended_cache: dict[str, _Cache] = {}


def get_extended_high_low_cached(
    symbol: str, lookback_days: float = EXTENDED_LOOKBACK_DAYS
) -> list[tuple[int, float, float]]:
    """Como `get_recent_high_low_cached`, pero con velas de `EXTENDED_INTERVAL`
    (5m) y una ventana de dias en vez de horas -- el tramo "historico" del
    heatmap. Cache y candado APARTE (misma `_lock_for`, con un prefijo en la
    clave para no compartir slot con el tramo reciente): son peticiones,
    TTL y volumen de datos distintos."""
    now = time.monotonic()
    entry = _extended_cache.get(symbol)
    if entry and now - entry.fetched_at < EXTENDED_CACHE_TTL_SECONDS:
        return entry.data

    with _lock_for(f"ext:{symbol}"):
        now = time.monotonic()
        entry = _extended_cache.get(symbol)
        if entry and now - entry.fetched_at < EXTENDED_CACHE_TTL_SECONDS:
            return entry.data
        data = _fetch_extended_high_low(symbol, lookback_days)
        _extended_cache[symbol] = _Cache(data=data, fetched_at=time.monotonic())
        return data
