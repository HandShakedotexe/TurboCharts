"""Logica pura (sin DB, sin red) del paper trading "DAY TRADING":
dimensionado de la posicion, simulacion de una orden contra velas de 1
minuto, y estadisticas de la cuenta.

Por que aparte del router: la simulacion es lo que hay que poder probar sin
Binance ni Postgres (ver tests/test_paper_model.py), y es donde viven las
decisiones de honestidad del simulador -- cada una documentada abajo.

Convencion de honestidad (la unica que importa en paper trading: un
simulador optimista da una cuenta de papel que no se parece a la real):

* Una vela de 1m trae maximo y minimo pero no el ORDEN en que ocurrieron.
  Si la misma vela toca SL y TP, se asume SL (`ambiguous_exit=True`).
* En la vela donde una orden pendiente se llena, solo se evalua el SL (el
  maximo/minimo de esa vela pudo ocurrir antes de la entrada). El TP se
  evalua desde la vela siguiente.
* Un gap a traves del SL sale al precio de apertura (peor que el SL); un gap
  a favor a traves del TP tambien sale al precio de apertura (mejor).
* Comision por lado (`fee_pct`, % del nocional) en entrada y salida; sin
  slippage ni funding (se documenta en la UI como N/A, no se inventa).
"""
from __future__ import annotations

from typing import Optional

MINUTE_MS = 60_000

# open_ms, open, high, low, close -- una vela de 1m de Binance.
Kline = tuple[int, float, float, float, float]

DEFAULT_SETTINGS = {
    "initial_balance": 100.0,
    "fee_pct": 0.04,          # taker de Binance Futures USD-M, por lado
    "default_risk_pct": 5.0,  # % del balance que se arriesga por trade (distancia al SL)
    "max_leverage": 20.0,     # tope de nocional / balance: protege de SL muy cercanos
    "analyst_min_trades": 20,
}


def session_for(ts_ms: int) -> str:
    """Sesion de mercado por hora UTC. Aproximada a proposito (los limites
    reales se solapan): solo sirve para agrupar operaciones parecidas."""
    hour = (ts_ms // 3_600_000) % 24
    if hour < 7:
        return "asia"
    if hour < 13:
        return "londres"
    if hour < 21:
        return "nueva_york"
    return "cierre"


def planned_rr(side: str, entry: float, sl: float, tp: float) -> Optional[float]:
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return abs(tp - entry) / risk


def rr_bucket(rr: Optional[float]) -> str:
    if rr is None:
        return "?"
    if rr < 1:
        return "<1"
    if rr < 2:
        return "1-2"
    if rr < 3:
        return "2-3"
    return ">=3"


def validate_levels(side: str, entry: float, sl: float, tp: float) -> None:
    if side not in ("long", "short"):
        raise ValueError("side debe ser 'long' o 'short'")
    if min(entry, sl, tp) <= 0:
        raise ValueError("los precios deben ser positivos")
    if side == "long" and not (sl < entry < tp):
        raise ValueError("en un LONG el SL debe estar debajo de la entrada y el TP arriba")
    if side == "short" and not (tp < entry < sl):
        raise ValueError("en un SHORT el TP debe estar debajo de la entrada y el SL arriba")


def size_position(
    balance: float, entry: float, sl: float, *, risk_pct: float,
    risk_usd: Optional[float], max_leverage: float,
) -> dict:
    """Cantidad para que perder el SL cueste `risk_usd` (o `risk_pct` del
    balance). Si el nocional pasa de `balance * max_leverage` se recorta la
    cantidad y `capped=True` -- el riesgo real queda por debajo del pedido."""
    if balance <= 0:
        raise ValueError("el balance de la cuenta es cero o negativo")
    distance = abs(entry - sl)
    if distance <= 0:
        raise ValueError("la entrada y el SL no pueden ser iguales")
    wanted_risk = risk_usd if risk_usd is not None else balance * risk_pct / 100.0
    if wanted_risk <= 0:
        raise ValueError("el riesgo debe ser positivo")
    qty = wanted_risk / distance
    capped = False
    max_notional = balance * max_leverage
    if qty * entry > max_notional:
        qty = max_notional / entry
        capped = True
    return {
        "qty": qty,
        "notional_usd": qty * entry,
        "risk_usd": qty * distance,
        "risk_pct": qty * distance / balance * 100.0,
        "leverage": qty * entry / balance,
        "capped": capped,
    }


def close_math(side: str, entry: float, exit_price: float, qty: float, fee_pct: float, risk_usd: float) -> dict:
    sign = 1.0 if side == "long" else -1.0
    gross = (exit_price - entry) * qty * sign
    fees = (entry * qty + exit_price * qty) * fee_pct / 100.0
    net = gross - fees
    return {
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "r_multiple": net / risk_usd if risk_usd > 0 else None,
    }


def excursions(side: str, entry: float, qty: float, high: Optional[float], low: Optional[float]) -> dict:
    """MFE/MAE en USD (ambos positivos), desde el maximo/minimo desde la
    entrada. None si la orden nunca se lleno."""
    if high is None or low is None:
        return {"mfe_usd": None, "mae_usd": None}
    if side == "long":
        return {"mfe_usd": max(0.0, (high - entry) * qty), "mae_usd": max(0.0, (entry - low) * qty)}
    return {"mfe_usd": max(0.0, (entry - low) * qty), "mae_usd": max(0.0, (high - entry) * qty)}


def first_bar_ms(created_ms: int) -> int:
    """Primera vela de 1m que se evalua: la SIGUIENTE a la de creacion. La
    vela en curso ya trae movimiento previo a la orden, y contarlo la
    llenaria/pararia con precios que el usuario no pudo operar."""
    return (created_ms // MINUTE_MS + 1) * MINUTE_MS


def simulate(trade: dict, klines: list[Kline]) -> dict:
    """Avanza una orden `pending`/`open` con velas de 1m ordenadas. Devuelve
    SOLO los campos que cambian (para un UPDATE) -- `{}` si nada cambio.

    Campos de `trade` que lee: side, status, entry_price, sl_price, tp_price,
    fill_bar_ms, high_since_fill, low_since_fill, last_checked_ms. Es
    idempotente: re-evaluar la ultima vela (aun en formacion) no cambia el
    resultado, porque maximo/minimo solo crecen y SL/TP, una vez tocados,
    siguen tocados.
    """
    status = trade["status"]
    if status not in ("pending", "open"):
        return {}
    side = trade["side"]
    entry = float(trade["entry_price"])
    sl = float(trade["sl_price"])
    tp = float(trade["tp_price"])
    long_ = side == "long"
    fill_bar_ms = trade.get("fill_bar_ms")
    high = trade.get("high_since_fill")
    low = trade.get("low_since_fill")
    filled_at_ms = trade.get("filled_at_ms")
    start_ms = trade.get("last_checked_ms") or 0
    out: dict = {}
    last_seen: Optional[int] = None

    for open_ms, o, h, l, _c in klines:
        if open_ms < start_ms:
            continue
        last_seen = open_ms
        is_fill_bar = False
        if status == "pending":
            if not (l <= entry <= h):
                continue
            status = "open"
            filled_at_ms = open_ms
            fill_bar_ms = open_ms
            high = entry
            low = entry
            is_fill_bar = True
            out["status"] = "open"
            out["filled_at_ms"] = open_ms
            out["fill_bar_ms"] = open_ms
        else:
            is_fill_bar = fill_bar_ms is not None and open_ms == fill_bar_ms
            if not is_fill_bar:
                high = h if high is None else max(high, h)
                low = l if low is None else min(low, l)

        sl_hit = (l <= sl) if long_ else (h >= sl)
        tp_hit = ((h >= tp) if long_ else (l <= tp)) and not is_fill_bar
        if sl_hit or tp_hit:
            ambiguous = sl_hit and tp_hit
            if sl_hit:
                # Gap a traves del SL: sale al open (peor que el SL). En la
                # vela de relleno el open es anterior a la entrada, no aplica.
                gapped = (not is_fill_bar) and ((o < sl) if long_ else (o > sl))
                exit_price = o if gapped else sl
                reason = "sl"
            else:
                gapped = (o > tp) if long_ else (o < tp)
                exit_price = o if gapped else tp
                reason = "tp"
            out.update(
                status="closed", exit_price=exit_price, exit_reason=reason,
                closed_at_ms=open_ms, ambiguous_exit=ambiguous,
            )
            status = "closed"
            break

    if high is not None:
        out["high_since_fill"] = high
        out["low_since_fill"] = low
    if last_seen is not None:
        out["last_checked_ms"] = last_seen
    return out


def compute_summary(settings: dict, trades: list[dict]) -> dict:
    """Estadisticas de la cuenta. `trades` con los campos de la tabla; solo
    los `closed` cuentan para P&L. Curva de equity: un punto por cierre, en
    orden de `closed_at_ms`, mas el punto inicial."""
    initial = float(settings["initial_balance"])
    closed = sorted(
        (t for t in trades if t["status"] == "closed" and t.get("net_pnl") is not None),
        key=lambda t: t["closed_at_ms"],
    )
    equity = initial
    peak = initial
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    curve: list[dict] = []
    if closed:
        curve.append({"time_ms": closed[0].get("filled_at_ms") or closed[0]["closed_at_ms"], "equity": initial})
    wins = losses = 0
    gross_win = gross_loss = 0.0
    r_values: list[float] = []
    for t in closed:
        pnl = float(t["net_pnl"])
        equity += pnl
        if pnl >= 0:
            wins += 1
            gross_win += pnl
        else:
            losses += 1
            gross_loss += -pnl
        if t.get("r_multiple") is not None:
            r_values.append(float(t["r_multiple"]))
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd_usd:
            max_dd_usd = dd
            max_dd_pct = dd / peak * 100.0 if peak > 0 else 0.0
        curve.append({"time_ms": t["closed_at_ms"], "equity": equity})
    n = len(closed)
    return {
        "initial_balance": initial,
        "balance": equity,
        "net_pnl": equity - initial,
        "return_pct": (equity - initial) / initial * 100.0 if initial > 0 else 0.0,
        "closed_trades": n,
        "open_trades": sum(1 for t in trades if t["status"] == "open"),
        "pending_trades": sum(1 for t in trades if t["status"] == "pending"),
        "win_rate": wins / n if n else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win": gross_win / wins if wins else None,
        "avg_loss": -gross_loss / losses if losses else None,
        "expectancy_r": sum(r_values) / len(r_values) if r_values else None,
        "max_drawdown_usd": max_dd_usd,
        "max_drawdown_pct": max_dd_pct,
        "equity_curve": curve,
    }
