"""Pollers REST: open interest, funding y datos de referencia.

Criterio de diseño: UNA llamada masiva por exchange siempre que exista, en vez
de una por símbolo. Bybit y OKX exponen endpoints que devuelven todos los
instrumentos de golpe, así que grabamos el mercado entero por el precio de una
petición. Binance no tiene bulk para open interest, así que ahí sí hay que
enumerar, y por eso `top_symbols` existe.

Los snapshots de referencia (especificaciones de contrato) se graban también.
El ctVal de un contrato de OKX o el tick size de un símbolo de Binance cambian
con el tiempo, y el histórico de esos cambios tampoco se puede recuperar luego.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from .rest import get_json
from .writer import RawWriter, utc_ms

log = logging.getLogger(__name__)


class AllFetchesFailed(Exception):
    """Ningún elemento del lote se pudo obtener.

    Se levanta en lugar de escribir un registro compuesto solo de errores: eso
    ocuparía sitio en raw/ pareciendo un dato cuando no lo es. El fallo queda
    en los contadores del poller y en el log, que es donde corresponde. Un
    lote PARCIAL sí se graba, con sus marcas _error dentro, porque ahí la parte
    buena es información real y la mala es contexto.
    """


def _require_some_success(items: list, key: str = "_error"):
    if items and all(isinstance(i, dict) and key in i for i in items):
        raise AllFetchesFailed(f"{len(items)} de {len(items)} peticiones fallaron")
    return items


class Poller:
    """Ejecuta `fetch` cada `interval` segundos y graba lo que devuelva."""

    def __init__(self, name: str, writer: RawWriter, fetch: Callable, interval: float):
        self.name = name
        self.writer = writer
        self.fetch = fetch
        self.interval = interval
        self._stop = asyncio.Event()
        self.ok = 0
        self.errors = 0
        self.last_ok_at: Optional[int] = None
        self.last_error: Optional[str] = None

    async def run(self) -> None:
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                payload = await self.fetch()
                if payload is not None:
                    self.writer.write(payload, received_at=utc_ms())
                    self.ok += 1
                    self.last_ok_at = utc_ms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("[poller %s] %s", self.name, self.last_error)
            elapsed = asyncio.get_running_loop().time() - started
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.5, self.interval - elapsed))
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "poller": self.name,
            "ok": self.ok,
            "errors": self.errors,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------- fetchers
def binance_premium_index(rest_url: str):
    """Bulk: markPrice, indexPrice, lastFundingRate y nextFundingTime de TODO."""
    async def _f():
        return await get_json(f"{rest_url}/fapi/v1/premiumIndex")
    return _f


def binance_open_interest(rest_url: str, symbols: list[str]):
    """Binance no tiene bulk de open interest: hay que ir símbolo a símbolo."""
    async def _f():
        out = []
        for sym in symbols:
            try:
                out.append(await get_json(f"{rest_url}/fapi/v1/openInterest?symbol={sym}"))
            except Exception as exc:
                out.append({"symbol": sym, "_error": f"{type(exc).__name__}: {exc}"[:200]})
            await asyncio.sleep(0.05)   # respirar entre peticiones
        return {"fetched_at": utc_ms(), "openInterest": _require_some_success(out)}
    return _f


def bybit_tickers(rest_url: str):
    """Bulk: en una sola llamada vienen openInterest, fundingRate y markPrice."""
    async def _f():
        return await get_json(f"{rest_url}/v5/market/tickers?category=linear")
    return _f


def okx_open_interest(rest_url: str):
    async def _f():
        return await get_json(f"{rest_url}/api/v5/public/open-interest?instType=SWAP")
    return _f


def okx_mark_price(rest_url: str):
    async def _f():
        return await get_json(f"{rest_url}/api/v5/public/mark-price?instType=SWAP")
    return _f


def okx_funding(rest_url: str, symbols: list[str]):
    async def _f():
        out = []
        for inst in symbols:
            try:
                out.append(await get_json(f"{rest_url}/api/v5/public/funding-rate?instId={inst}"))
            except Exception as exc:
                out.append({"instId": inst, "_error": f"{type(exc).__name__}: {exc}"[:200]})
            await asyncio.sleep(0.05)
        return {"fetched_at": utc_ms(), "funding": _require_some_success(out)}
    return _f


def reference_snapshot(binance_rest: str, bybit_rest: str, okx_rest: str):
    """Especificaciones de contrato de los tres exchanges, juntas."""
    async def _f():
        out = {"fetched_at": utc_ms()}
        for key, url in (
            ("binance_exchange_info", f"{binance_rest}/fapi/v1/exchangeInfo"),
            ("bybit_instruments", f"{bybit_rest}/v5/market/instruments-info?category=linear&limit=1000"),
            ("okx_instruments", f"{okx_rest}/api/v5/public/instruments?instType=SWAP"),
        ):
            try:
                out[key] = await get_json(url, timeout=30)
            except Exception as exc:
                out[key] = {"_error": f"{type(exc).__name__}: {exc}"[:300]}
        _require_some_success([v for k, v in out.items() if k != "fetched_at"])
        return out
    return _f
