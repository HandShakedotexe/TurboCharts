"""GET /api/heatmap -- heatmap de liquidaciones MODELADO, NO CALIBRADO.

Ver api/heatmap_model.py para la formula y, sobre todo, para los supuestos
(cesta de apalancamientos, reparto 50/50 long/short) que NO son datos
medidos. Cualquier consumidor de esta respuesta debe mostrar `label` de
forma visible -- es la etiqueta requerida mientras el modelo no este
calibrado contra el stream completo de Bybit (Fase 4).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backtest.indicators import liquidation_cluster_proximity

from .. import bigtrades, pricehistory
from ..db import get_conn
from ..heatmap_model import (
    DEFAULT_BASKET,
    DEFAULT_BIN_WIDTH_USD,
    MmrTable,
    MmrTier,
    bin_levels_by_leverage,
    compute_levels,
    compute_levels_from_trades,
    compute_liquidity_intervals,
    filter_consumed_levels,
)

router = APIRouter()

# Ancho de celda "por defecto" que ve el frontend cuando no hay una celda
# real bajo el cursor (ver HeatmapOverlay.tsx, fallback del contorno de
# hover) -- ya NO controla el calculo de la cuadricula (ver
# heatmap_model.compute_liquidity_intervals, que da a cada celda su propia
# duracion EXACTA, no un bucket fijo). Se mantiene solo como valor
# informativo/de respaldo.
GRID_BUCKET_MS = 300_000

# Cuanto extender el heatmap en vivo hacia atras con big trades HISTORICOS
# (tabla `big_trades`, poblada por ingest/binance_historical.py +
# db/load_historical.py). Subido de 7 a 90 dias (2026-08-31) para que
# coincida con la profundidad real del backfill de BTCUSDT -- con 7 dias,
# extender el backfill a 3 meses no cambiaba nada visible: esta constante
# seguia recortando la
# consulta a la ultima semana sin importar cuanto mas hubiera en la tabla.
# Solo aplica al modo EN VIVO (as_of=None); el modo historico con `as_of`
# explicito no lo usa, fuera de alcance de este cambio.
HISTORICAL_LOOKBACK_DAYS = 90

# Umbral (en horas desde `as_of`) a partir del cual los big trades
# historicos se agregan por hora + bin de $100 en vez de usarse uno a uno.
# Medido, no adivinado (2026-08-31): 90 dias de BTCUSDT son ~1.76M trades
# reales -- procesarlos uno a uno tarda ~22s solo en compute_levels_from_trades
# (17.6M niveles). Agregados a (hora, bin de $100) quedan en ~10,000 filas
# y el mismo calculo tarda ~0.1s -- una perdida de granularidad aceptable
# para liquidez de hace semanas (el frontend no puede pintar detalle de
# un solo trade en un pixel que representa dias de todos modos). El tramo
# reciente (menor a este umbral) SIGUE siendo trade a trade, sin agregar.
AGGREGATE_HISTORICAL_TRADES_OLDER_THAN_HOURS = 24.0

# Recalcular el heatmap en vivo es caro: con una semana de historico son
# ~700,000 niveles y el analisis de decaimiento escanea miles de barras de
# precio -- medido en pruebas reales (2026-08-30), varios segundos por
# calculo AISLADO, y mucho mas bajo carga concurrente (Python es
# single-threaded para trabajo CPU-bound: varias pestañas sondeando a la
# vez, cada una recalculando desde cero, se pisan entre si y cada peticion
# se vuelve mas lenta cuantas mas corran al mismo tiempo). Cachear la
# RESPUESTA COMPLETA, no solo sus partes (bigtrades/pricehistory ya se
# cacheaban por separado, pero el ensamblado final -- compute_levels_from_trades
# + compute_liquidity_intervals -- no). TTL menor que el poll de 20s del
# frontend (App.tsx HEATMAP_POLL_MS) para que cada ciclo de poll normal
# dispare como mucho un recalculo real.
RESPONSE_CACHE_TTL_SECONDS = 15.0


def _latest_price(conn, symbol: str, as_of: datetime):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_time, close FROM candles
               WHERE exchange='binance' AND symbol=%s AND available_at <= %s
               ORDER BY event_time DESC LIMIT 1""",
            (symbol, as_of),
        )
        return cur.fetchone()


def _latest_oi(conn, symbol: str, as_of: datetime) -> Optional[float]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT sum_open_interest_value FROM open_interest_metrics
               WHERE exchange='binance' AND symbol=%s AND available_at <= %s
                 AND sum_open_interest_value IS NOT NULL
               ORDER BY event_time DESC LIMIT 1""",
            (symbol, as_of),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _live_price_and_oi(symbol: str) -> tuple[float, float, float]:
    """Precio, open interest y funding EN VIVO, directo a Binance.

    No hay snapshot de "ahora" en la DB -- `candles` y `open_interest_metrics`
    solo tienen meses ya cerrados (ver api/routers/live.py). Calcular el
    heatmap contra un precio de hace semanas lo deja centrado en el sitio
    equivocado del grafico: justo lo que se vio al probarlo por primera vez.

    El funding sale de la MISMA respuesta de `premiumIndex` que ya se pedia
    para el precio (trae `lastFundingRate`) -- para `market_snapshot`
    (§2 del plan de esta fase) no hace falta una llamada nueva.
    """
    from .live import BINANCE_REST, _get_json

    mark = _get_json(f"{BINANCE_REST}/fapi/v1/premiumIndex?symbol={symbol}")
    price = float(mark["markPrice"])
    funding_rate = float(mark["lastFundingRate"])
    oi_raw = _get_json(f"{BINANCE_REST}/fapi/v1/openInterest?symbol={symbol}")
    # openInterest viene en el activo base (p.ej. BTC), no en USD -- hay que
    # pasarlo a notional para que sea comparable con sum_open_interest_value.
    oi_notional = float(oi_raw["openInterest"]) * price
    return price, oi_notional, funding_rate


def _live_long_short_ratio(symbol: str) -> Optional[float]:
    """Long/short ratio global EN VIVO -- fail-soft: es un extra del
    panel lateral (§2/§6 del plan), no algo de lo que dependa el resto de
    la respuesta del heatmap."""
    from .live import BINANCE_REST, _get_json

    try:
        rows = _get_json(
            f"{BINANCE_REST}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
        )
        return float(rows[0]["longShortRatio"]) if rows else None
    except Exception:
        return None


def _mmr_table(conn, symbol: str, as_of: datetime) -> MmrTable:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT notional_floor, notional_cap, maintenance_margin_rate, max_leverage
               FROM margin_brackets
               WHERE exchange='binance' AND symbol=%s AND fetched_at = (
                   SELECT max(fetched_at) FROM margin_brackets
                   WHERE exchange='binance' AND symbol=%s AND fetched_at <= %s
               )
               ORDER BY bracket_seq""",
            (symbol, symbol, as_of),
        )
        rows = cur.fetchall()
    return MmrTable([MmrTier(*r) for r in rows])


def _historical_big_trades(
    conn, symbol: str, start: datetime, end: datetime
) -> tuple[list[tuple[float, float, int]], list[tuple[float, float, int]]]:
    """Big trades archivados en `big_trades` (ver ingest/binance_historical.py
    + db/load_historical.py) con `available_at` en `[start, end)`. Mismo
    formato de 3-tupla `(precio, notional_usd, trade_time_ms)` que
    `bigtrades.get_recent_big_trades_cached`.

    Devuelve DOS listas, `(agregados, crudos)`, en vez de una sola -- ver
    AGGREGATE_HISTORICAL_TRADES_OLDER_THAN_HOURS. El tramo `crudos` (mas
    reciente dentro de esta ventana historica) sigue siendo trade a trade;
    el tramo `agregados` (mas viejo) se agrega en SQL por hora + bin de
    $100 (cada fila agregada se pasa como si fuera un solo "trade
    sintetico" de notional grande -- `compute_levels_from_trades` no
    distingue uno de otro). Esto reduce el VOLUMEN DE NIVELES de entrada
    (de ~1.76M trades reales a ~10,000 filas agregadas) independientemente
    de como se calcule despues la cuadricula -- ver
    heatmap_model.compute_liquidity_intervals, que ya no necesita un
    `price_history` de resolucion distinta por tramo."""
    aggregate_cutoff = end - timedelta(hours=AGGREGATE_HISTORICAL_TRADES_OLDER_THAN_HOURS)
    aggregated: list[tuple[float, float, int]] = []
    raw: list[tuple[float, float, int]] = []

    if aggregate_cutoff > start:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date_trunc('hour', event_time) AS bucket_time,
                          round(price / 100.0) * 100.0 AS price_bucket,
                          sum(notional_usd) AS total_notional
                   FROM big_trades
                   WHERE exchange='binance' AND symbol=%s
                     AND available_at >= %s AND available_at < %s
                   GROUP BY bucket_time, price_bucket
                   ORDER BY bucket_time""",
                (symbol, start, aggregate_cutoff),
            )
            aggregated = [
                (price_bucket, notional, int(bucket_time.timestamp() * 1000))
                for bucket_time, price_bucket, notional in cur.fetchall()
            ]

    raw_start = max(start, aggregate_cutoff)
    if end > raw_start:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT price, notional_usd, event_time FROM big_trades
                   WHERE exchange='binance' AND symbol=%s
                     AND available_at >= %s AND available_at < %s
                   ORDER BY event_time""",
                (symbol, raw_start, end),
            )
            raw = [
                (price, notional, int(event_time.timestamp() * 1000))
                for price, notional, event_time in cur.fetchall()
            ]
    return aggregated, raw


def _build_heatmap_response(symbol: str, as_of: Optional[datetime], bin_size: float) -> dict:
    """El trabajo real detras de `GET /api/heatmap` -- separado del handler
    de la ruta para que este pueda envolverlo en el cache de respuesta (ver
    `RESPONSE_CACHE_TTL_SECONDS`) sin mezclar las dos responsabilidades."""
    is_live = as_of is None
    as_of = as_of or datetime.now(timezone.utc)
    price_time = as_of

    historical_aggregated: list[tuple[float, float, int]] = []
    historical_raw: list[tuple[float, float, int]] = []
    with get_conn() as conn:
        mmr_table = _mmr_table(conn, symbol, as_of)

        if is_live:
            try:
                price, oi, funding_rate = _live_price_and_oi(symbol)
            except Exception as exc:
                raise HTTPException(502, f"no se pudo obtener precio/OI en vivo para {symbol}: {exc}") from exc
            # Extiende la ventana de trades hacia atras con lo que ya
            # aterrizo en `big_trades` (ingesta historica) -- solo el tramo
            # MAS VIEJO que la ventana en vivo de bigtrades.LOOKBACK_HOURS,
            # para no duplicar los mismos trades desde dos fuentes. Si la
            # tabla todavia no existe o esta vacia (backfill no corrido
            # todavia), esto simplemente no aporta nada -- fail-soft, el
            # heatmap en vivo de siempre no depende de esto.
            try:
                live_cutoff = as_of - timedelta(hours=bigtrades.LOOKBACK_HOURS)
                historical_aggregated, historical_raw = _historical_big_trades(
                    conn, symbol, as_of - timedelta(days=HISTORICAL_LOOKBACK_DAYS), live_cutoff
                )
            except Exception:
                historical_aggregated, historical_raw = [], []
        else:
            price_row = _latest_price(conn, symbol, as_of)
            oi = _latest_oi(conn, symbol, as_of)
            if not price_row or oi is None:
                raise HTTPException(404, f"sin datos suficientes para {symbol} en as_of={as_of.isoformat()}")
            price_time, price = price_row

    # Con trades grandes reales el heatmap sale de MUCHAS entradas (una por
    # trade, a su propio precio) en vez de una sola sintetica desde el
    # precio actual -- ver docstring de compute_levels_from_trades para el
    # porque. Si no hay trades grandes en la ventana (simbolo poco liquido,
    # o modo historico con `as_of`), se cae al modelo anterior antes que
    # devolver un heatmap vacio.
    live_trades = bigtrades.get_recent_big_trades_cached(symbol) if is_live else []
    recent_trades = historical_raw + live_trades
    all_trades = historical_aggregated + recent_trades
    price_history: list[tuple[int, float, float]] = []
    grid_cells_by_leverage: dict[int, list] = {}
    if all_trades:
        recent_levels = compute_levels_from_trades(recent_trades, mmr_table, basket=DEFAULT_BASKET)
        historical_levels = compute_levels_from_trades(historical_aggregated, mmr_table, basket=DEFAULT_BASKET)
        levels = recent_levels + historical_levels
        # Decaimiento: un nivel generado desde un trade real desaparece en
        # cuanto el precio ya lo cruzo despues de ese trade (ver docstring de
        # filter_consumed_levels). Si Binance no responde para esta llamada
        # extra, se prefiere mostrar el heatmap SIN decaimiento a no mostrar
        # heatmap -- es una mejora de presentacion sobre datos ya validos,
        # no algo de lo que dependa la respuesta.
        try:
            recent_history = pricehistory.get_recent_high_low_cached(symbol)
            extended_history = pricehistory.get_extended_high_low_cached(symbol) if is_live else []
            # Recorta el tramo extendido a ANTES de donde empieza el
            # reciente -- ambos piden "hasta ahora", asi que sin este corte
            # se solaparian (5m y 1m para la misma ventana) y la lista
            # concatenada dejaria de estar ordenada de forma ascendente,
            # que es lo que exige compute_liquidity_intervals/filter_consumed_levels.
            recent_start_ms = recent_history[0][0] if recent_history else None
            if recent_start_ms is not None:
                extended_history = [h for h in extended_history if h[0] < recent_start_ms]
            price_history = extended_history + recent_history

            # UNA sola pasada, con TODOS los niveles contra el `price_history`
            # completo (hasta 90 dias, resolucion de 5 min sin resamplear) --
            # ver heatmap_model.compute_liquidity_intervals, que encuentra el
            # cruce exacto de cada nivel con busqueda binaria (O(log barras))
            # en vez del escaneo lineal que obligaba a resamplear a
            # resoluciones cada vez mas gruesas para mantener el costo
            # bajo. Cada celda trae
            # su propia duracion EXACTA (`GridCell.bucket_ms`), no un ancho
            # de bucket fijo -- el decaimiento queda pegado a la barra real
            # donde el precio cruzo el nivel, no a un paso de horas/dias.
            grid_cells = compute_liquidity_intervals(levels, price_history, bin_size=bin_size)
            for cell in grid_cells:
                grid_cells_by_leverage.setdefault(cell.leverage, []).append(cell)
            # El filtrado de consumidos (para el snapshot de "ahora", no la
            # cuadricula) SI puede usar el price_history completo -- es
            # barato: solo necesita saber SI se cruzo, no EN QUE bucket
            # exacto (sin el escaneo lineal que hace caro a la cuadricula).
            levels = filter_consumed_levels(levels, price_history)
        except Exception:
            pass
    else:
        levels = compute_levels(price, oi, mmr_table, basket=DEFAULT_BASKET)
    by_leverage = bin_levels_by_leverage(levels, bin_size=bin_size)

    all_leverages = sorted(set(by_leverage) | set(grid_cells_by_leverage))
    tiers = [
        {
            "leverage": leverage,
            "levels": [{"price": p, "notional_usd": n}
                       for p, n in sorted(by_leverage.get(leverage, {}).items())],
            "cells": [{"time_ms": c.time_ms, "price": c.price, "notional_usd": c.notional_usd,
                       "bucket_ms": c.bucket_ms}
                      for c in sorted(grid_cells_by_leverage.get(leverage, []),
                                       key=lambda c: (c.time_ms, c.price))],
        }
        for leverage in all_leverages
    ]

    # Snapshot de mercado para el panel lateral (§2/§6 del plan de esta
    # fase): datos que NO tiene sentido dibujar como una celda mas del
    # heatmap (un solo numero por instante, no una serie precio x tiempo).
    # Reusa `levels`/`price` que esta funcion YA calculo arriba -- cero
    # trabajo nuevo de modelo, solo lo que faltaba pedir (funding y
    # long/short vinieron de _live_price_and_oi/_live_long_short_ratio).
    # Solo en modo vivo: en modo historico (`as_of` explicito) no hay
    # funding/long-short "de ese instante" que pedirle a Binance en vivo,
    # y no vale la pena la complejidad de archivarlo aqui todavia.
    market_snapshot = None
    if is_live:
        market_snapshot = {
            "funding_rate": funding_rate,
            "oi_notional_usd": oi,
            "long_short_ratio": _live_long_short_ratio(symbol),
            "cluster_proximity": liquidation_cluster_proximity(price, levels),
        }

    return {
        "label": "MODELED . UNCALIBRATED",
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "reference_price": price,
        "reference_price_time": price_time.isoformat(),
        "open_interest_usd": oi,
        "market_snapshot": market_snapshot,
        "basket": {"name": DEFAULT_BASKET.name, "version": DEFAULT_BASKET.version,
                   "weights": DEFAULT_BASKET.weights},
        "source": "big_trades" if all_trades else "open_interest",
        "big_trades_count": len(all_trades),
        "live_big_trades_count": len(live_trades),
        "historical_big_trades_count": len(historical_raw) + len(historical_aggregated),
        "historical_big_trades_aggregated_count": len(historical_aggregated),
        "grid_bucket_ms": GRID_BUCKET_MS,
        "bin_size_usd": bin_size,
        "big_trade_threshold_usd": bigtrades.BIG_TRADE_THRESHOLD_USD,
        "tiers": tiers,
    }


@dataclass
class _ResponseCache:
    data: dict
    fetched_at: float = 0.0


# Solo el modo EN VIVO se cachea aqui -- as_of explicito ya es barato
# (compute_levels sobre una sola entrada sintetica, no compute_levels_from_trades
# sobre miles de trades) y cada valor de as_of es tipicamente distinto entre
# llamadas, asi que cachearlo no ayudaria.
_response_cache: dict[str, _ResponseCache] = {}
_response_locks: dict[str, threading.Lock] = {}
_response_locks_guard = threading.Lock()


def _response_lock_for(key: str) -> threading.Lock:
    with _response_locks_guard:
        if key not in _response_locks:
            _response_locks[key] = threading.Lock()
        return _response_locks[key]


def get_cached_market_snapshot(symbol: str, bin_size: float = DEFAULT_BIN_WIDTH_USD) -> Optional[dict]:
    """Lee `market_snapshot` (funding, cluster de liquidacion, ver
    `_build_heatmap_response`) del cache de respuesta de ESTE symbolo sin
    forzar un recalculo -- usado por `api/routers/backtest.py` para el
    estado en vivo de las estrategias (§4 del plan de esta fase). Fail-soft
    a `None` si todavia no hay una respuesta cacheada fresca (p.ej. nadie
    abrio el heatmap de ese simbolo todavia) en vez de disparar el calculo
    caro de `_build_heatmap_response` solo para un ping de estado."""
    entry = _response_cache.get(f"{symbol}:{bin_size}")
    if not entry or time.monotonic() - entry.fetched_at >= RESPONSE_CACHE_TTL_SECONDS:
        return None
    return entry.data.get("market_snapshot")


@router.get("/heatmap")
def get_heatmap(
    symbol: str = Query(...),
    as_of: Optional[datetime] = Query(
        None, description="ISO 8601 para un punto historico; sin esto usa precio y OI EN VIVO"
    ),
    bin_size: float = Query(
        DEFAULT_BIN_WIDTH_USD, gt=0, description="ancho del bin de precio, en dolares (cuadricula fija)"
    ),
) -> dict:
    if as_of is not None:
        return _build_heatmap_response(symbol, as_of, bin_size)

    cache_key = f"{symbol}:{bin_size}"
    now = time.monotonic()
    entry = _response_cache.get(cache_key)
    if entry and now - entry.fetched_at < RESPONSE_CACHE_TTL_SECONDS:
        return entry.data

    with _response_lock_for(cache_key):
        now = time.monotonic()
        entry = _response_cache.get(cache_key)
        if entry and now - entry.fetched_at < RESPONSE_CACHE_TTL_SECONDS:
            return entry.data
        data = _build_heatmap_response(symbol, None, bin_size)
        _response_cache[cache_key] = _ResponseCache(data=data, fetched_at=time.monotonic())
        return data
