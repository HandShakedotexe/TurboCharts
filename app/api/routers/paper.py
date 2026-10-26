"""API del paper trading "DAY TRADING".

Primer router de la app que ESCRIBE (POST/PUT/PATCH) -- las ordenes simuladas
y los ajustes viven en las tablas `paper_*` (ver db/schema.sql). La logica
(dimensionado, simulacion contra velas, estadisticas) esta en
`api/paper_model.py` y el bot analista en `api/paper_analyst.py`, ambas puras
y probadas sin DB; este archivo solo hace SQL + traer velas de Binance.

No hay proceso en segundo plano: las ordenes `pending`/`open` se avanzan
("se resuelven") cuando alguien pide la lista (`GET /paper/trades`), con las
velas de 1m desde la ultima revision. Es lo mas simple que funciona porque la
simulacion es determinista respecto a las velas: da igual cuando se calcule,
siempre que se calcule con todas las velas desde la creacion.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import get_conn
from ..paper_analyst import analyze
from ..paper_model import (
    DEFAULT_SETTINGS, MINUTE_MS, Kline, close_math, compute_summary, excursions, first_bar_ms,
    planned_rr, rr_bucket, session_for, simulate, size_position, validate_levels,
)
from .live import BINANCE_REST, _fetch_since, _get_json

router = APIRouter()

# Minimo entre dos resoluciones de ordenes: la pagina de Bots pide la lista
# seguido, y cada resolucion es una llamada a Binance por simbolo con ordenes
# vivas (el proyecto ya tuvo un 429 real por exceso de peticiones).
_REFRESH_MIN_INTERVAL_S = 5.0
_refresh_lock = threading.Lock()
_last_refresh = 0.0

# Columnas que `simulate`/el cierre pueden escribir -- lista blanca para
# armar el UPDATE (los nombres vienen de nuestro codigo, nunca del cliente).
_UPDATABLE = frozenset({
    "status", "filled_at_ms", "fill_bar_ms", "exit_price", "exit_reason", "closed_at_ms",
    "ambiguous_exit", "high_since_fill", "low_since_fill", "last_checked_ms",
    "gross_pnl", "fees", "net_pnl", "r_multiple",
})


class SettingsIn(BaseModel):
    initial_balance: Optional[float] = Field(None, gt=0)
    fee_pct: Optional[float] = Field(None, ge=0, le=5)
    default_risk_pct: Optional[float] = Field(None, gt=0, le=100)
    max_leverage: Optional[float] = Field(None, gt=0, le=125)
    analyst_min_trades: Optional[int] = Field(None, ge=2, le=1000)


class TradeIn(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=20)
    interval: str = Field(..., min_length=1, max_length=5)
    side: Literal["long", "short"]
    entry_price: float = Field(..., gt=0)
    sl_price: float = Field(..., gt=0)
    tp_price: float = Field(..., gt=0)
    # market: entra ya al precio actual (ignora `entry_price` salvo para
    # recordar de donde vino); limit: espera a que el precio toque la entrada.
    order_type: Literal["market", "limit"] = "limit"
    risk_pct: Optional[float] = Field(None, gt=0, le=100)
    risk_usd: Optional[float] = Field(None, gt=0)
    note: str = Field(..., min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    confidence: Optional[int] = Field(None, ge=1, le=5)
    source_position_id: Optional[str] = Field(None, max_length=80)


class TradePatch(BaseModel):
    note: Optional[str] = Field(None, min_length=1, max_length=4000)
    tags: Optional[list[str]] = Field(None, max_length=12)
    confidence: Optional[int] = Field(None, ge=1, le=5)
    review_note: Optional[str] = Field(None, max_length=4000)
    followed_plan: Optional[bool] = None


def _rows(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_settings(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT initial_balance, fee_pct, default_risk_pct, max_leverage, analyst_min_trades
                       FROM paper_settings WHERE id = 1""")
        rows = _rows(cur)
    return rows[0] if rows else dict(DEFAULT_SETTINGS)


def _current_balance(conn, initial: float) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(net_pnl), 0) FROM paper_trades WHERE status = 'closed'")
        return initial + float(cur.fetchone()[0])


def _last_price(symbol: str) -> float:
    data = _get_json(f"{BINANCE_REST}/fapi/v1/ticker/price?symbol={symbol}")
    return float(data["price"])


def _apply_updates(conn, trade_id: int, updates: dict) -> None:
    updates = {k: v for k, v in updates.items() if k in _UPDATABLE}
    if not updates:
        return
    sets = ", ".join(f"{k} = %s" for k in updates)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE paper_trades SET {sets} WHERE trade_id = %s", [*updates.values(), trade_id])


def _refresh(conn, settings: dict, force: bool = False) -> Optional[str]:
    """Avanza las ordenes vivas con velas de 1m. Devuelve un texto de error si
    algun simbolo no se pudo consultar (la lista se sirve igual, con lo ultimo
    que se sabia)."""
    global _last_refresh
    with _refresh_lock:
        now = time.time()
        if not force and now - _last_refresh < _REFRESH_MIN_INTERVAL_S:
            return None
        _last_refresh = now
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE status IN ('pending', 'open') ORDER BY trade_id")
            live = _rows(cur)
        by_symbol: dict[str, list[dict]] = {}
        for t in live:
            by_symbol.setdefault(t["symbol"], []).append(t)
        error: Optional[str] = None
        for symbol, trades in by_symbol.items():
            start_ms = min(t["last_checked_ms"] for t in trades)
            try:
                raw = _fetch_since(symbol, "1m", start_ms)
            except HTTPException as exc:
                error = f"{symbol}: {exc.detail}"
                continue
            klines: list[Kline] = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw]
            for t in trades:
                updates = simulate(t, klines)
                if updates.get("status") == "closed":
                    updates.update(close_math(
                        t["side"], t["entry_price"], updates["exit_price"], t["qty"],
                        settings["fee_pct"], t["risk_usd"],
                    ))
                _apply_updates(conn, t["trade_id"], updates)
        conn.commit()
        return error


def _serialize(t: dict) -> dict:
    out = dict(t)
    out["tags"] = list(t.get("tags") or [])
    rr = planned_rr(t["side"], t["entry_price"], t["sl_price"], t["tp_price"])
    out["planned_rr"] = rr
    out["rr_bucket"] = rr_bucket(rr)
    out["leverage"] = t["notional_usd"] / t["balance_at_open"] if t.get("balance_at_open") else None
    out.update(excursions(t["side"], t["entry_price"], t["qty"], t.get("high_since_fill"), t.get("low_since_fill")))
    return out


def _all_trades(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM paper_trades ORDER BY trade_id DESC")
        return [_serialize(t) for t in _rows(cur)]


def _get_trade(conn, trade_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM paper_trades WHERE trade_id = %s", (trade_id,))
        rows = _rows(cur)
    if not rows:
        raise HTTPException(404, f"no existe el trade #{trade_id}")
    return rows[0]


@router.get("/paper/settings")
def get_settings() -> dict:
    with get_conn() as conn:
        return _load_settings(conn)


@router.put("/paper/settings")
def put_settings(body: SettingsIn) -> dict:
    changes = body.model_dump(exclude_none=True)
    with get_conn() as conn:
        if changes:
            sets = ", ".join(f"{k} = %s" for k in changes)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE paper_settings SET {sets}, updated_at = now() WHERE id = 1", list(changes.values()))
            conn.commit()
        return _load_settings(conn)


@router.get("/paper/trades")
def list_trades(refresh: bool = True) -> dict:
    with get_conn() as conn:
        settings = _load_settings(conn)
        error = _refresh(conn, settings, force=False) if refresh else None
        trades = _all_trades(conn)
        return {
            "trades": trades,
            "summary": compute_summary(settings, trades),
            "settings": settings,
            "refresh_error": error,
        }


@router.post("/paper/trades", status_code=201)
def create_trade(body: TradeIn) -> dict:
    symbol = body.symbol.upper()
    with get_conn() as conn:
        settings = _load_settings(conn)
        balance = _current_balance(conn, settings["initial_balance"])
        created_ms = int(time.time() * 1000)
        entry = body.entry_price
        if body.order_type == "market":
            try:
                entry = _last_price(symbol)
            except (KeyError, ValueError, TypeError) as exc:  # respuesta rara de Binance: no se inventa un precio
                raise HTTPException(502, f"no se pudo leer el precio actual de {symbol}: {exc}") from exc
        try:
            validate_levels(body.side, entry, body.sl_price, body.tp_price)
            sizing = size_position(
                balance, entry, body.sl_price,
                risk_pct=body.risk_pct if body.risk_pct is not None else settings["default_risk_pct"],
                risk_usd=body.risk_usd, max_leverage=settings["max_leverage"],
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        market = body.order_type == "market"
        when = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
        context = {
            "session": session_for(created_ms),
            "weekday": when.strftime("%A").lower(),
            "hour_utc": when.hour,
        }
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO paper_trades
                     (created_ms, symbol, interval, side, status, order_type, entry_price, sl_price, tp_price,
                      qty, notional_usd, risk_usd, risk_pct, capped, balance_at_open, note, tags, confidence,
                      source_position_id, filled_at_ms, fill_bar_ms, high_since_fill, low_since_fill,
                      last_checked_ms, context)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING trade_id""",
                (
                    created_ms, symbol, body.interval, body.side, "open" if market else "pending",
                    body.order_type, entry, body.sl_price, body.tp_price, sizing["qty"], sizing["notional_usd"],
                    sizing["risk_usd"], sizing["risk_pct"], sizing["capped"], balance, body.note.strip(),
                    [t.strip() for t in body.tags if t.strip()], body.confidence, body.source_position_id,
                    created_ms if market else None,
                    created_ms // MINUTE_MS * MINUTE_MS if market else None,
                    entry if market else None, entry if market else None,
                    first_bar_ms(created_ms), json.dumps(context),
                ),
            )
            trade_id = cur.fetchone()[0]
        conn.commit()
        return _serialize(_get_trade(conn, trade_id))


@router.patch("/paper/trades/{trade_id}")
def patch_trade(trade_id: int, body: TradePatch) -> dict:
    changes = body.model_dump(exclude_unset=True)
    with get_conn() as conn:
        _get_trade(conn, trade_id)
        if changes:
            if changes.get("note") is None:
                changes.pop("note", None)  # la columna es NOT NULL: null no borra el motivo
            if changes.get("tags") is None:
                changes.pop("tags", None)
            else:
                changes["tags"] = [t.strip() for t in changes["tags"] if t.strip()]
        if changes:
            sets = ", ".join(f"{k} = %s" for k in changes)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE paper_trades SET {sets} WHERE trade_id = %s", [*changes.values(), trade_id])
            conn.commit()
        return _serialize(_get_trade(conn, trade_id))


@router.post("/paper/trades/{trade_id}/close")
def close_trade(trade_id: int) -> dict:
    """Cierre manual al precio actual (solo ordenes `open`)."""
    with get_conn() as conn:
        settings = _load_settings(conn)
        _refresh(conn, settings, force=True)  # que SL/TP pendientes de aplicar no se pierdan
        t = _get_trade(conn, trade_id)
        if t["status"] == "pending":
            raise HTTPException(409, "la orden todavia no se lleno: cancelala en vez de cerrarla")
        if t["status"] != "open":
            raise HTTPException(409, f"el trade ya esta {t['status']}")
        price = _last_price(t["symbol"])
        now_ms = int(time.time() * 1000)
        updates = {
            "status": "closed", "exit_price": price, "exit_reason": "manual", "closed_at_ms": now_ms,
            **close_math(t["side"], t["entry_price"], price, t["qty"], settings["fee_pct"], t["risk_usd"]),
        }
        hi, lo = t.get("high_since_fill"), t.get("low_since_fill")
        updates["high_since_fill"] = price if hi is None else max(hi, price)
        updates["low_since_fill"] = price if lo is None else min(lo, price)
        _apply_updates(conn, trade_id, updates)
        conn.commit()
        return _serialize(_get_trade(conn, trade_id))


@router.post("/paper/trades/{trade_id}/cancel")
def cancel_trade(trade_id: int) -> dict:
    with get_conn() as conn:
        settings = _load_settings(conn)
        _refresh(conn, settings, force=True)  # puede que ya se haya llenado
        t = _get_trade(conn, trade_id)
        if t["status"] != "pending":
            raise HTTPException(409, f"solo se cancelan ordenes pendientes (esta {t['status']})")
        _apply_updates(conn, trade_id, {
            "status": "cancelled", "exit_reason": "cancelled", "closed_at_ms": int(time.time() * 1000),
        })
        conn.commit()
        return _serialize(_get_trade(conn, trade_id))


@router.get("/paper/analysis")
def get_analysis(force: bool = False) -> dict:
    with get_conn() as conn:
        settings = _load_settings(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY trade_id")
            closed = [_serialize(t) for t in _rows(cur)]
        return analyze(closed, min_trades=settings["analyst_min_trades"], force=force)
