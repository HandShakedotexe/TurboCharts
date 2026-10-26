"""GET /api/backtest/runs -- resultados de las corridas del backtester,
para la vista "Bots" del frontend. Solo lectura: las corridas se generan
con `python -m backtest --save` (ver app/backtest/__main__.py), no aqui.

GET /api/backtest/strategies -- catalogo de estrategias registradas
(titulo, descripcion, direccion, tamano de orden) MAS un estado en vivo
("Analizando" / "Condiciones cumplidas", con el detalle de que se esta
evaluando) -- ver `Strategy.live_status`. Importante: este estado
describe que evaluaria la estrategia AHORA MISMO si estuviera plana,
contra el dato mas reciente disponible, no una posicion real abierta."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backtest import indicators as backtest_indicators
from backtest import regime as backtest_regime
from backtest import ta as backtest_ta
from backtest.data import FundingPoint, OiPoint, load_bars, load_funding, load_oi_metrics
from backtest.journal import load_run, load_validation, load_validation_trades, load_validations
from backtest.strategies import STRATEGIES

from ..db import get_conn
from .heatmap import get_cached_market_snapshot

router = APIRouter()

# Cuantos dias hacia atras se pide `funding_rate` para el z-score en vivo
# (ver backtest.indicators.funding_zscore, lookback interno de 8 eventos).
# 60, no 4: se descubrio (2026-09-01, verificado con una consulta directa)
# que la tabla `funding_rate` de BTCUSDT no tiene filas mas nuevas que
# hace mas de un mes -- el job de ingesta que la llena parece detenido,
# aunque el grabador de liquidaciones siga corriendo. Con una
# ventana de 4 dias esto devolvia SIEMPRE 0 filas (la ventana entera caia
# despues del ultimo dato real) y el status quedaba en "sin_datos" para
# siempre. 60 dias llega con margen hasta datos reales; el z-score sale
# igual comparando el funding EN VIVO contra una distribucion de hace un
# mes, que es peor que tenerla fresca pero mejor que no tener nada -- vale
# la pena que alguien revise por que la ingesta de funding se detuvo (no es
# parte de este cambio, es un hallazgo aparte).
_FUNDING_ZSCORE_LOOKBACK_DAYS = 60


@router.get("/backtest/runs")
def list_runs(limit: int = Query(50, le=200)) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.run_id, r.started_at, r.symbol, r.interval, r.strategy,
                      r.data_start, r.data_end,
                      count(t.trade_seq) FILTER (WHERE t.pnl_usd IS NOT NULL) AS trades,
                      count(t.trade_seq) FILTER (WHERE t.pnl_usd > 0) AS wins,
                      coalesce(sum(t.pnl_usd), 0) AS total_pnl_usd
               FROM backtest_runs r
               LEFT JOIN backtest_trades t ON t.run_id = r.run_id
               GROUP BY r.run_id
               ORDER BY r.started_at DESC
               LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "run_id": run_id, "started_at": started_at.isoformat(), "symbol": symbol,
            "interval": interval, "strategy": strategy,
            "data_start": data_start.isoformat(), "data_end": data_end.isoformat(),
            "trades": trades, "win_rate": (wins / trades) if trades else 0.0,
            "total_pnl_usd": total_pnl_usd,
        }
        for (run_id, started_at, symbol, interval, strategy, data_start, data_end,
             trades, wins, total_pnl_usd) in rows
    ]


@router.get("/backtest/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with get_conn() as conn:
        try:
            return load_run(conn, run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


def _validation_to_json(row: dict) -> dict:
    # started_at/data_start/data_end son datetime, win_rate_ci/expectancy_r_ci
    # se recomponen en tuplas para que el frontend las lea igual que ya lee
    # el JSON que imprime `--validate` por consola (mismo shape, ver
    # validation.report_to_dict) -- una sola forma de representar "un
    # reporte de validacion" en todo el proyecto, CLI o API.
    out = {
        **{k: v for k, v in row.items() if k not in (
            "started_at", "data_start", "data_end",
            "win_rate_ci_low", "win_rate_ci_high",
            "expectancy_r_ci_low", "expectancy_r_ci_high",
        )},
        "started_at": row["started_at"].isoformat(),
        "data_start": row["data_start"].isoformat(),
        "data_end": row["data_end"].isoformat(),
        "win_rate_ci_95": [row["win_rate_ci_low"], row["win_rate_ci_high"]],
        "expectancy_r_ci_95": (
            [row["expectancy_r_ci_low"], row["expectancy_r_ci_high"]]
            if row["expectancy_r_ci_low"] is not None else None
        ),
        # Validaciones guardadas ANTES de una migracion que agrego estas
        # columnas no las tienen -- `{}` en vez de `None` para que el
        # frontend no tenga que chequear null en cada lectura,
        # mismo criterio que "mes/semana sin trades simplemente no aparece"
        # que ya usan `monthly_usd_per_day`/`weekly_usd_per_week`.
        "monthly_usd_per_day": row.get("monthly_usd_per_day") or {},
        "weekly_usd_per_week": row.get("weekly_usd_per_week") or {},
    }
    return out


@router.get("/backtest/validations")
def list_validations() -> list[dict]:
    with get_conn() as conn:
        return [_validation_to_json(row) for row in load_validations(conn)]


@router.get("/backtest/validations/{validation_id}")
def get_validation(validation_id: str) -> dict:
    with get_conn() as conn:
        try:
            row = load_validation(conn, validation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        # Trades OOS reales, mismo shape que `GET /backtest/runs/{id}`
        # devuelve para una corrida normal -- ver load_validation_trades.
        trades = load_validation_trades(conn, validation_id)
    return {**_validation_to_json(row), "trades": trades}


def _live_funding_zscore(conn, symbol: str) -> Optional[float]:
    """Funding z-score EN VIVO: historico reciente (`funding_rate`, tabla
    archivada) mas el valor actual (directo a Binance, mismo endpoint que
    ya usa heatmap.py::_live_price_and_oi) -- fail-soft a None, es un
    extra del panel de estado, no algo de lo que dependa el resto de la
    respuesta."""
    try:
        from .live import BINANCE_REST, _get_json

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_FUNDING_ZSCORE_LOOKBACK_DAYS)
        rows = load_funding(conn, symbol, start, end)
        mark = _get_json(f"{BINANCE_REST}/fapi/v1/premiumIndex?symbol={symbol}")
        live_point = FundingPoint(int(end.timestamp() * 1000), float(mark["lastFundingRate"]))
        points = [*rows, live_point]
        if len(points) < 2:
            return None
        return backtest_indicators.funding_zscore(points)[-1]
    except Exception:
        return None


def _live_oi_trend_and_regime(
    conn, symbol: str,
) -> tuple[Optional[float], Optional[float], Optional[str], Optional[float]]:
    """OI y regimen "en vivo" -- a diferencia del funding z-score (que si
    completa con una llamada directa a Binance, ver _live_funding_zscore),
    esto usa SOLO lo ya archivado (`open_interest_metrics`/`candles`):
    mezclar `sum_open_interest` (base, lo que ya usa el motor de verdad
    en un backtest) con `sumOpenInterestValue` (USD, lo unico que expone
    el endpoint REST de cola en vivo, ver live.fetch_oi_history_since)
    daria una tendencia con unidades inconsistentes -- peor que quedarse
    con datos un poco menos frescos. Puede quedar hasta un dia desfasado
    de "ahora" si el poll del backfill se atraso -- aceptable para una
    tarjeta informativa, fail-soft a (None, None)."""
    try:
        # 60 dias, no 7: `candles` archivado solo se completa con el
        # volcado MENSUAL de data.binance.vision, publicado despues de
        # que el mes cierra -- las velas pueden llegar con mas de un mes
        # de rezago respecto a "ahora", aunque `open_interest_metrics` se
        # mantenga mas al dia. Con 30 dias `load_bars` seguia devolviendo
        # 0 filas -- el regimen es el que de verdad necesita margen, ya
        # que solo se rellena con el volcado mensual, a diferencia del
        # funding y el OI que este mismo router SI completa con una cola
        # en vivo (ver _live_funding_zscore). Es un hueco operativo del
        # backfill, no algo que arregle este cambio.
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=60)
        oi_rows = load_oi_metrics(conn, symbol, start, end)
        bars = load_bars(conn, symbol, "1h", start, end)
        if len(oi_rows) < 13 or len(bars) < 20:
            return None, None, None, None
        oi_trend = backtest_indicators.oi_trend(oi_rows)[-1]
        oi_trend_zscore = backtest_indicators.oi_trend_zscore(oi_rows)[-1]
        regime = backtest_regime.classify_regime(bars)[-1]
        # RSI reusa los mismos `bars` de 1h/60d ya cargados arriba para el
        # regimen -- no hace falta una segunda consulta, ver
        # strategies/rsi_pullback.py.
        rsi = backtest_ta.rsi(bars)[-1]
        return oi_trend, oi_trend_zscore, regime, rsi
    except Exception:
        return None, None, None, None


@router.get("/backtest/strategies")
def list_strategies(symbol: str = Query("BTCUSDT")) -> list[dict]:
    with get_conn() as conn:
        funding_zscore = _live_funding_zscore(conn, symbol)
        oi_trend, oi_trend_zscore, regime, rsi = _live_oi_trend_and_regime(conn, symbol)
    market_snapshot = get_cached_market_snapshot(symbol)
    cluster = market_snapshot.get("cluster_proximity") if market_snapshot else None

    # Un solo diccionario para TODAS las estrategias registradas -- cada
    # `live_status` (ver Strategy.live_status en cada archivo de
    # strategies/) lee solo las claves que necesita y devuelve "sin_datos"
    # si le faltan las suyas, en vez de que el router adivine que necesita
    # cada estrategia con un chequeo por-clave aqui.
    live_snapshot = {
        "funding_zscore": funding_zscore, "cluster": cluster,
        "oi_trend": oi_trend, "oi_trend_zscore": oi_trend_zscore, "regime": regime,
        "rsi": rsi,
    }

    out = []
    for key, cls in STRATEGIES.items():
        strat = cls()
        out.append({
            "key": key,
            "title": strat.title,
            "description": strat.description,
            "direction": strat.direction,
            "size_usd": strat.size_usd,
            "status": strat.live_status(live_snapshot),
        })
    return out
