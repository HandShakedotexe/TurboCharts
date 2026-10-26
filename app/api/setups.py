"""Panel de decision en vivo para el "pool fade": combina una liquidacion
REAL reciente y significativa con la evidencia de que ocurrio cerca de un
pool de liquidez denso (varios apalancamientos convergiendo en el mismo
precio -- la "correlacion entre X") y lo confirma con CVD/funding/OI en
vivo.

Es UNA VERSION DEL PROCESO MANUAL DEL USUARIO llevada a la pantalla, NO
una estrategia validada -- ningun setup de aqui paso por el arnes de
validacion (walk-forward, trials_count, Benjamini-Hochberg, ver
backtest/validation.py). Con liquidaciones reales grabadas desde hace
poco, no hay ni de lejos la muestra necesaria para correr ese arnes
contra esto todavia -- eso queda para cuando el grabador acumule meses,
no dias. Las "confirmaciones" son informativas, no un filtro: no
descartan un setup, el usuario decide.
"""
from __future__ import annotations

from typing import Optional

from .real_liquidations import get_recent_liquidations_cached

# "Considerable" -- mismo umbral que bigtrades.BIG_TRADE_THRESHOLD_USD,
# ya verificado contra datos reales de BTCUSDT como "genuinamente
# grande, no ruido de retail" (ver bigtrades.py).
DEFAULT_MIN_LIQUIDATION_USD = 100_000.0
# Umbral de notional minimo del pool para considerarlo denso.
DEFAULT_MIN_POOL_USD = 50_000_000.0
# Al menos 2 apalancamientos DISTINTOS convergiendo -- la "correlacion
# entre X", la señal de un pool real, no un nivel aislado de un solo
# apalancamiento.
DEFAULT_MIN_CORRELATION = 2
# +/- N bins de precio (bin_size_usd del heatmap, normalmente $100)
# alrededor del precio de la liquidacion, para juntar niveles de
# apalancamientos distintos que no caen en el MISMO bin exacto pero estan
# claramente en la misma zona.
DEFAULT_PRICE_TOLERANCE_BINS = 3
# TP al precio de entrada +/- 0.5% a 1%, contemplando costos de
# entrada/ejecucion.
DEFAULT_TP_PCT_LOW = 0.005
DEFAULT_TP_PCT_HIGH = 0.01


def _pool_near(tiers: list[dict], price: float, at_time_ms: int, tolerance_usd: float) -> dict[int, float]:
    """Notional modelado por apalancamiento en un rango de +/-
    `tolerance_usd` alrededor de `price`, ACTIVO en el instante
    `at_time_ms` -- cuantos apalancamientos DISTINTOS tenian liquidez ahi
    justo antes de la liquidacion, no solo cuanto notional total (un pool
    "correlacionado" de verdad tiene varios apalancamientos, no uno solo
    con mucho volumen).

    Usa `tiers[].cells` (el historial de intervalos exactos, ver
    heatmap_model.compute_liquidity_intervals), NO `tiers[].levels` (el
    snapshot YA decaido de "ahora mismo"): por construccion, si el precio
    cruzo un nivel para gatillar la liquidacion, `levels` (que ya aplico
    `filter_consumed_levels`) YA NO lo tiene -- pedirle a `levels` "que
    pool habia justo aqui" en el momento exacto de una liquidacion real
    da SIEMPRE cero, porque ese pool es justo el que se acaba de
    consumir. `cells` conserva la duracion exacta de cada intervalo
    (`time_ms` a `time_ms + bucket_ms`), asi que se puede preguntar "que
    seguia activo en el instante ANTES de que esto pasara", que es la
    pregunta real."""
    per_leverage: dict[int, float] = {}
    for tier in tiers:
        total = sum(
            cell["notional_usd"] for cell in tier["cells"]
            if abs(cell["price"] - price) <= tolerance_usd
            and cell["time_ms"] <= at_time_ms < cell["time_ms"] + cell["bucket_ms"]
        )
        if total > 0:
            per_leverage[tier["leverage"]] = total
    return per_leverage


def _recent_cvd_trend(symbol: str, bars: int = 20) -> Optional[float]:
    """Delta neto de CVD (compra taker menos venta taker) de las ultimas
    `bars` velas de 5m -- NO es el CVD acumulado completo
    (`/api/indicators/cvd`, que arranca desde 2020): aqui solo interesa la
    tendencia MUY reciente, para confirmar o contradecir un rebote que
    acaba de empezar. Fail-soft a None -- es una confirmacion, no algo de
    lo que dependa el resto de la respuesta."""
    try:
        from .routers.live import BINANCE_REST, _get_json, _parse_rows

        raw = _get_json(f"{BINANCE_REST}/fapi/v1/klines?symbol={symbol}&interval=5m&limit={bars}")
        rows = _parse_rows(raw)
        net = 0.0
        for r in rows:
            taker_sell = r["volume"] - r["taker_buy_volume"]
            net += r["taker_buy_volume"] - taker_sell
        return net
    except Exception:
        return None


def find_setups(
    symbol: str,
    min_liquidation_usd: float = DEFAULT_MIN_LIQUIDATION_USD,
    min_pool_usd: float = DEFAULT_MIN_POOL_USD,
    min_correlation: int = DEFAULT_MIN_CORRELATION,
    price_tolerance_bins: int = DEFAULT_PRICE_TOLERANCE_BINS,
) -> dict:
    # Import tardio: heatmap.py y backtest.py tambien importan de aqui en
    # otras direcciones en algun punto de la cadena -- tardio evita
    # cualquier ciclo de import al cargar el modulo, sin cambiar nada del
    # comportamiento (son funciones normales, no rutas que dependan de
    # orden de registro).
    from .heatmap_model import DEFAULT_BIN_WIDTH_USD
    from .routers.backtest import _live_funding_zscore, _live_oi_trend_and_regime
    from .routers.heatmap import get_heatmap
    from .db import get_conn

    events = get_recent_liquidations_cached(symbol, limit=200)
    big_events = [e for e in events if e.quote_qty >= min_liquidation_usd]
    if not big_events:
        return {"symbol": symbol, "setups": [], "checked_liquidations": len(events)}

    # `as_of`/`bin_size` SIEMPRE explicitos al llamar una funcion de ruta
    # directo (sin pasar por FastAPI) -- sin esto, el valor por defecto real
    # es el propio objeto `fastapi.Query(...)` sin resolver, no `None`/100.0
    # (mismo gotcha ya documentado en tests/test_live.py para
    # get_live_klines; se repitio aqui, verificado con el traceback real:
    # `psycopg.ProgrammingError: cannot adapt type 'Query'`, no adivinado).
    heatmap = get_heatmap(symbol=symbol, as_of=None, bin_size=DEFAULT_BIN_WIDTH_USD)
    tiers = heatmap["tiers"]
    bin_size = heatmap["bin_size_usd"]
    tolerance_usd = bin_size * price_tolerance_bins

    with get_conn() as conn:
        funding_zscore = _live_funding_zscore(conn, symbol)
        oi_trend, _oi_trend_zscore, regime = _live_oi_trend_and_regime(conn, symbol)
    cvd_trend = _recent_cvd_trend(symbol)

    setups = []
    for e in big_events:
        pool = _pool_near(tiers, e.price, e.event_time, tolerance_usd)
        total_pool_usd = sum(pool.values())
        correlation = len(pool)
        if total_pool_usd < min_pool_usd or correlation < min_correlation:
            continue

        # side="long" liquidado -> el precio CAYO para gatillarlo -> la
        # tesis de rebote apuesta a que SUBE (fade = entrar LARGO).
        # Espejo para "short" (el precio SUBIO, tesis de rebote a la baja).
        fade_direction = "long" if e.side == "long" else "short"
        entry = e.price
        # Mismo criterio que _pool_near: celdas ACTIVAS en el instante de
        # la liquidacion, no el snapshot ya decaido -- para el borde del
        # pool que define el SL.
        pool_prices = [
            cell["price"] for tier in tiers for cell in tier["cells"]
            if abs(cell["price"] - entry) <= tolerance_usd
            and cell["time_ms"] <= e.event_time < cell["time_ms"] + cell["bucket_ms"]
        ]
        if fade_direction == "long":
            # SL justo debajo del borde del pool (no del precio de entrada) --
            # si el precio sigue bajando MAS ALLA del pool que se supone lo
            # sostiene, la tesis del rebote ya fallo.
            stop = (min(pool_prices) - bin_size) if pool_prices else entry * (1 - 0.01)
            target_low = entry * (1 + DEFAULT_TP_PCT_LOW)
            target_high = entry * (1 + DEFAULT_TP_PCT_HIGH)
        else:
            stop = (max(pool_prices) + bin_size) if pool_prices else entry * (1 + 0.01)
            target_low = entry * (1 - DEFAULT_TP_PCT_LOW)
            target_high = entry * (1 - DEFAULT_TP_PCT_HIGH)

        # Confirmaciones: informativas, NUNCA filtran un setup -- combinan
        # con otros indicadores para dar señales mas claras, sin que el
        # sistema decida por el usuario.
        confirmations: list[str] = []
        if funding_zscore is not None:
            if fade_direction == "long" and funding_zscore <= -1.5:
                confirmations.append(
                    f"Funding z-score {funding_zscore:.2f} (cortos apretados) -- confirma el rebote alcista.",
                )
            elif fade_direction == "short" and funding_zscore >= 1.5:
                confirmations.append(
                    f"Funding z-score {funding_zscore:.2f} (largos apretados) -- confirma el rebote bajista.",
                )
        if cvd_trend is not None:
            if fade_direction == "long" and cvd_trend > 0:
                confirmations.append("CVD reciente comprador -- a favor del rebote.")
            elif fade_direction == "short" and cvd_trend < 0:
                confirmations.append("CVD reciente vendedor -- a favor del rebote.")
        if oi_trend is not None and oi_trend < 0:
            confirmations.append(
                "Open interest bajando -- consistente con liquidaciones reales cerrando posiciones, "
                "no solo movimiento de precio.",
            )

        setups.append({
            "trigger": {
                "event_time_ms": e.event_time,
                "side": e.side,
                "price": e.price,
                "quote_qty_usd": e.quote_qty,
            },
            "pool": {
                "total_notional_usd": total_pool_usd,
                "leverage_tiers": sorted(pool.keys()),
                "correlation": correlation,
                "per_leverage_usd": pool,
            },
            "suggested": {
                "direction": fade_direction,
                "entry": entry,
                "stop": stop,
                "target_low": target_low,
                "target_high": target_high,
            },
            "confirmations": confirmations,
            "market_context": {
                "funding_zscore": funding_zscore,
                "oi_trend": oi_trend,
                "regime": regime,
                "cvd_trend_recent": cvd_trend,
            },
        })

    setups.sort(key=lambda s: s["trigger"]["event_time_ms"], reverse=True)
    return {"symbol": symbol, "setups": setups, "checked_liquidations": len(events)}
