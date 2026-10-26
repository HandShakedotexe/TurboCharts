"""Datos "en vivo" -- NO persisten en TimescaleDB, se piden directo a
Binance en cada llamada.

Los volcados historicos (`candles` en la DB) solo cubren meses ya cerrados
(data.binance.vision no publica el mes en curso hasta que termina). Sin
esto, el grafico siempre tendria un hueco de hasta cuatro semanas entre el
ultimo dato archivado y hoy, que es justo lo que se noto al probarlo por
primera vez: "el precio no se actualiza en tiempo real".
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

BINANCE_REST = "https://fapi.binance.com"
USER_AGENT = "liq-intel-api/0.1"
MAX_PAGES = 30  # de sobra: 30 * 1500 velas de 5m son mas de 3 meses


# 3 intentos, no 1 (2026-09-14, reporte del usuario con captura: un solo
# HTTP 502 al cambiar a 1h dejaba el grafico roto sin forma de recuperarse
# sola). Un timeout/URLError aislado contra la API publica de Binance es
# ruido de red normal, no un fallo real -- confirmado reproduciendo la
# MISMA peticion segundos despues: exito, 200 OK, mismos datos. Backoff
# corto (0.5s, 1s) para no convertir un blip en una espera larga, pero
# suficiente para que un problema transitorio del lado de Binance ya no
# este ahi en el segundo o tercer intento.
_RETRY_DELAYS_SECONDS = (0.5, 1.0)


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: Optional[urllib.error.URLError] = None
    for delay in (0.0, *_RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            last_exc = exc
    raise HTTPException(502, f"no se pudo contactar a Binance tras 3 intentos: {last_exc}") from last_exc


def _parse_rows(raw: list) -> list[dict]:
    return [
        {
            "time": int(row[6]) // 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            # row[9] = taker buy base volume -- ya viene en la respuesta de
            # Binance, no es una llamada nueva. Hace falta para poder seguir
            # acumulando el CVD (api/indicators.py) en la cola en vivo sin
            # corte al llegar a la vela en curso, ver `backtest.indicators.cvd`.
            "taker_buy_volume": float(row[9]),
        }
        for row in raw
    ]


def _fetch_since(symbol: str, interval: str, start_ms: int) -> list:
    """Pagina hacia adelante desde `start_ms` hasta ahora. Hace falta:
    con velas de 5 minutos, un hueco de un mes son ~8600 velas y el limite
    de Binance por llamada es 1500 -- una sola peticion con `limit` alto NO
    alcanza, sin importar que tan alto se pida `limit`. `limit` fijo (sin
    startTime) fue el bug real: cubria un numero de velas fijo sin importar
    cuanto tiempo representaba eso en cada temporalidad."""
    out: list = []
    cursor = start_ms
    now_ms = int(time.time() * 1000)
    for _ in range(MAX_PAGES):
        url = f"{BINANCE_REST}/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={cursor}&limit=1500"
        page = _get_json(url)
        if not page:
            break
        out.extend(page)
        last_open_time = int(page[-1][0])
        if len(page) < 1500 or last_open_time >= now_ms:
            break
        cursor = last_open_time + 1
    return out


OI_MAX_PAGES = 10  # 10 * 500 horas a period=1h son mas de 200 dias -- de sobra para el hueco entre lo archivado y ahora


def fetch_oi_history_since(symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Cola en vivo de Open Interest -- `open_interest_metrics` (Fase 1)
    solo cubre volcados ya archivados, igual que `candles`. `period=1h` (no
    5m) porque el limite de Binance es 500 filas por llamada: a 5m son
    ~41h por pagina, a 1h son ~20 dias -- el hueco tipico entre el ultimo
    metrics cargado y "ahora" es de dias, no de horas. Devuelve
    [(timestamp_ms, sum_open_interest_value), ...] ascendente."""
    out: list[tuple[int, float]] = []
    cursor = start_ms
    now_ms = int(time.time() * 1000)
    for _ in range(OI_MAX_PAGES):
        url = (f"{BINANCE_REST}/futures/data/openInterestHist"
               f"?symbol={symbol}&period=1h&startTime={cursor}&limit=500")
        page = _get_json(url)
        if not page:
            break
        out.extend((int(row["timestamp"]), float(row["sumOpenInterestValue"])) for row in page)
        last_ts = int(page[-1]["timestamp"])
        if len(page) < 500 or last_ts >= now_ms:
            break
        cursor = last_ts + 1
    return out


def fetch_funding_history_since(symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Cola en vivo de funding -- eventos reales (no solo el ultimo valor
    de premiumIndex), para poder graficar la serie completa desde donde
    termina `funding_rate` archivado hasta ahora. Limit=1000 y funding
    cada 8h son mas de 300 dias por pagina -- de sobra, no hace falta
    paginar."""
    url = f"{BINANCE_REST}/fapi/v1/fundingRate?symbol={symbol}&startTime={start_ms}&limit=1000"
    page = _get_json(url)
    return [(int(row["fundingTime"]), float(row["fundingRate"])) for row in page]


@router.get("/live-klines")
def get_live_klines(
    symbol: str = Query(...),
    interval: str = Query("1h"),
    limit: int = Query(200, le=1500, description="sin `start`: las ultimas `limit` velas"),
    start: Optional[datetime] = Query(
        None, description="con esto, pagina desde aqui hasta ahora sin importar cuantas velas haga falta"
    ),
) -> list[dict]:
    """`time` es el close_time de cada vela, igual que en /api/candles --
    para la vela en curso, closeTime es el cierre PROGRAMADO, todavia en el
    futuro, y eso es correcto: es la misma convencion que ya usan las velas
    archivadas, no la hora actual.
    """
    if start is not None:
        start_ms = int(start.replace(tzinfo=start.tzinfo or timezone.utc).timestamp() * 1000)
        raw = _fetch_since(symbol, interval, start_ms)
    else:
        url = f"{BINANCE_REST}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        raw = _get_json(url)
    return _parse_rows(raw)
