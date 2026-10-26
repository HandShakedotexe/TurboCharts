"""Arnes de validacion -- aplica un criterio estadistico explicito en vez
de solo reportar numeros sueltos de una corrida:

    1. Cota inferior del IC95% de la expectancy en R > 0.
    2. `trials_count` obligatorio, con correccion por comparaciones
       multiples (Benjamini-Hochberg).
    3. Out-of-sample independiente como condicion NECESARIA, no un informe
       aparte -- aqui, walk-forward "anchored": la ventana de entrenamiento
       crece con ancla fija en el inicio de los datos, la de prueba avanza
       en pasos fijos y NUNCA se solapa con lo que ya se uso para elegir
       parametros.
    4. Win rate siempre con su intervalo (ver statistics.win_rate_ci).

Sin este modulo, cualquier resultado de una estrategia es solo una
afirmacion -- demostrar que un resultado es real es aplicar el criterio
completo, no solo reportar una metrica aislada.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

from . import data as data_mod
from . import statistics as stats_mod
from .engine import BacktestResult, Strategy, Trade, run_backtest_on_series

# Capital inicial de referencia para leer un reporte en terminos de "¿esto
# haria crecer $100 de verdad?" -- mismo numero que el resto del proyecto
# asume como base de capital en cada trade que corre (ej. `risk_usd_target`
# en las grillas de `__main__.py`), no un numero nuevo inventado aca.
STARTING_CAPITAL_USD = 100.0
# Umbrales de "% diario sobre el capital" para
# `consecutive_months_meeting_floor` -- la meta es ganar de forma
# consistente un pequeño % cada dia sin caer en drawdown grave, nunca un
# % diario grande (matematicamente imposible sostenido en el tiempo). Se
# reportan los 4 a la vez -- no hace falta elegir uno solo de antemano.
DAILY_FLOOR_PCTS = (0.5, 1.0, 2.0, 3.0)
# Mismo rango, pero SEMANAL: una estrategia de pocos trades de alta
# conviccion no tiene una meta DIARIA con sentido (la mayoria de los dias
# no hay ningun trade cerrado); semanal si.
WEEKLY_FLOOR_PCTS = (0.5, 1.0, 2.0, 3.0)


@dataclass
class WalkForwardSplit:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def anchored_walk_forward_splits(
    start: datetime, end: datetime, min_train_days: int, test_days: int,
) -> list[WalkForwardSplit]:
    """Ventana de entrenamiento ANCLADA en `start` (crece cada paso, nunca
    se descarta historia vieja) + ventana de prueba de tamaño fijo que
    avanza SIN solaparse con ninguna prueba anterior ni con el propio
    entrenamiento que la eligio -- la variante estandar de walk-forward
    cuando no hay motivo (p.ej. un regimen de mercado que cambio de raiz)
    para preferir una ventana deslizante de tamaño fijo."""
    splits = []
    train_end = start + timedelta(days=min_train_days)
    while train_end + timedelta(days=test_days) <= end:
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        splits.append(WalkForwardSplit(start, train_end, test_start, test_end))
        train_end = test_end
    return splits


def _slice_by_time(rows: Sequence, start_ms: int, end_ms: int) -> list:
    """Bars/FundingPoint/OiPoint/BigTrade son NamedTuple con `time_ms`
    como primer campo -- un slice comun sirve para las cuatro series sin
    volver a pedirlas a la DB por cada tramo del walk-forward."""
    return [r for r in rows if start_ms <= r.time_ms < end_ms]


@dataclass
class SweepResult:
    params: dict
    trades_count: int
    expectancy_r: Optional[float]
    expectancy_r_ci_95: Optional[tuple[float, float]]
    p_value: Optional[float]


def sweep_in_sample(
    strategy_factory: Callable[..., Strategy],
    param_grid: list[dict],
    symbol: str, interval: str,
    bars: list, funding_rows: list, oi_rows: list, big_trades: list, mmr_table,
    train_start: datetime, train_end: datetime,
    commission_bps: float, slippage_bps: float,
    htf_bars: Optional[list] = None,
) -> list[SweepResult]:
    """Corre CADA combinacion de `param_grid` sobre la ventana de
    entrenamiento -- `len(param_grid)` ES el `trials_count` real de esta
    busqueda (criterio 2), no una estimacion posterior: se cuenta ANTES de
    saber cual gana, asi que no hay forma de "olvidar" intentos que
    salieron mal.

    `htf_bars` (keyword-only, opcional -- agregado para
    `strategies/smc_confluence.py`, `needs_htf_context=True`): YA
    recortado al mismo tramo temporal que `bars` por el caller (ver
    `walk_forward_validate`), se pasa tal cual a `run_backtest_on_series`."""
    out = []
    for params in param_grid:
        strat = strategy_factory(**params)
        result = run_backtest_on_series(
            symbol, interval, train_start, train_end, bars, funding_rows, oi_rows,
            big_trades, mmr_table, strat, strat.__class__.__name__, params,
            commission_bps=commission_bps, slippage_bps=slippage_bps,
            htf_bars=htf_bars,
        )
        exp_r = stats_mod.expectancy_r(result.trades)
        ci = stats_mod.expectancy_r_ci(result.trades, seed=0)
        p = stats_mod.expectancy_r_p_value(result.trades, seed=0)
        closed = sum(1 for t in result.trades if t.pnl_usd is not None)
        out.append(SweepResult(params, closed, exp_r, ci, p))
    return out


def pick_best(sweep: list[SweepResult]) -> Optional[SweepResult]:
    """Mejor configuracion por la COTA INFERIOR del IC95% de la
    expectancy en R (no por el punto medio ni por el P&L total) -- una
    configuracion con expectancy alta pero pocos trades/mucha varianza
    pierde contra una mas modesta pero mas segura, que es justo el punto
    del criterio 1. Ignora configuraciones sin CI (cero trades en la
    ventana de entrenamiento)."""
    candidates = [s for s in sweep if s.expectancy_r_ci_95 is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.expectancy_r_ci_95[0])


@dataclass
class FoldResult:
    split: WalkForwardSplit
    trials_count: int
    chosen_params: Optional[dict]
    chosen_survives_bh: bool
    oos_trades: list[Trade]


@dataclass
class WalkForwardReport:
    symbol: str
    interval: str
    strategy_name: str
    folds: list[FoldResult]
    oos_trades: list[Trade]          # TODOS los trades out-of-sample, concatenados
    total_trials_count: int
    win_rate: float
    win_rate_ci_95: tuple[float, float]
    expectancy_r: Optional[float]
    expectancy_r_ci_95: Optional[tuple[float, float]]
    sample_size: str                 # "OK" o "INSUFFICIENT_SAMPLE"
    # PF/expectancy en USD sobre TODOS los trades OOS concatenados, igual
    # que win_rate/expectancy_r ya lo hacen arriba (existian antes solo
    # para una corrida unica desde `statistics.summarize`).
    profit_factor: Optional[float]
    expectancy_usd: Optional[float]
    # Caida maxima real de una cuenta de $100 (ver STARTING_CAPITAL_USD
    # arriba) corriendo TODOS los trades OOS en orden cronologico (los
    # tramos de un walk-forward anchored nunca se solapan y avanzan en el
    # tiempo, asi que `oos_trades` ya viene ordenado) -- fraccion
    # (0.10 = 10%), ver `statistics.max_drawdown`. `None` sin trades
    # cerrados.
    max_drawdown_pct: Optional[float]
    # $/dia NETO de cada mes calendario con al menos un trade cerrado --
    # ver `statistics.monthly_usd_per_day`.
    monthly_usd_per_day: dict[str, float]
    # $/SEMANA neto -- ver DAILY_FLOOR_PCTS/WEEKLY_FLOOR_PCTS arriba: la
    # vista semanal tiene sentido para una estrategia de pocos trades de
    # alta conviccion, donde una meta diaria no aplicaria. Ver
    # `statistics.weekly_usd_per_week`.
    weekly_usd_per_week: dict[str, float]


def walk_forward_validate(
    strategy_factory: Callable[..., Strategy],
    param_grid: list[dict],
    symbol: str, interval: str,
    bars: list, funding_rows: list, oi_rows: list, big_trades: list, mmr_table,
    start: datetime, end: datetime,
    min_train_days: int, test_days: int,
    commission_bps: float = 5.0, slippage_bps: float = 2.0,
    bh_alpha: float = 0.05,
    strategy_key: Optional[str] = None,
    htf_bars: Optional[list] = None,
) -> WalkForwardReport:
    """Orquesta el criterio completo: por cada tramo walk-forward, barre
    `param_grid` SOLO sobre la ventana de entrenamiento de ese tramo
    (criterio 2), elige la mejor por cota inferior de IC, aplica
    Benjamini-Hochberg sobre los p-valores del barrido para saber si esa
    eleccion sigue siendo significativa una vez corregida por cuantas se
    probaron, y la corre SOLO sobre la ventana de prueba (criterio 3, que
    nunca se solapa con el entrenamiento que la eligio). El resultado
    final junta los trades out-of-sample de TODOS los tramos -- la unica
    cifra que responde honestamente "funciono de verdad" (criterio 1)."""
    splits = anchored_walk_forward_splits(start, end, min_train_days, test_days)
    folds: list[FoldResult] = []
    all_oos_trades: list[Trade] = []
    total_trials = 0

    for split in splits:
        train_start_ms = int(split.train_start.timestamp() * 1000)
        train_end_ms = int(split.train_end.timestamp() * 1000)
        test_start_ms = int(split.test_start.timestamp() * 1000)
        test_end_ms = int(split.test_end.timestamp() * 1000)

        train_bars = _slice_by_time(bars, train_start_ms, train_end_ms)
        train_funding = _slice_by_time(funding_rows, train_start_ms, train_end_ms)
        train_oi = _slice_by_time(oi_rows, train_start_ms, train_end_ms)
        train_trades_data = _slice_by_time(big_trades, train_start_ms, train_end_ms)
        # `htf_bars` es una serie MAS CORTA que `bars` (menos velas por
        # ser de temporalidad mayor) pero mismo esquema `time_ms` --
        # `_slice_by_time` ya generico le sirve igual que a las otras
        # cuatro series de arriba.
        train_htf_bars = _slice_by_time(htf_bars, train_start_ms, train_end_ms) if htf_bars else None

        sweep = sweep_in_sample(
            strategy_factory, param_grid, symbol, interval,
            train_bars, train_funding, train_oi, train_trades_data, mmr_table,
            split.train_start, split.train_end, commission_bps, slippage_bps,
            htf_bars=train_htf_bars,
        )
        total_trials += len(param_grid)
        best = pick_best(sweep)
        survivors = stats_mod.benjamini_hochberg([s.p_value for s in sweep], alpha=bh_alpha)
        best_idx = sweep.index(best) if best is not None else -1
        chosen_survives = best_idx >= 0 and survivors[best_idx]

        oos_trades: list[Trade] = []
        if best is not None:
            test_bars = _slice_by_time(bars, test_start_ms, test_end_ms)
            test_funding = _slice_by_time(funding_rows, test_start_ms, test_end_ms)
            test_oi = _slice_by_time(oi_rows, test_start_ms, test_end_ms)
            test_trades_data = _slice_by_time(big_trades, test_start_ms, test_end_ms)
            test_htf_bars = _slice_by_time(htf_bars, test_start_ms, test_end_ms) if htf_bars else None
            strat = strategy_factory(**best.params)
            oos_result = run_backtest_on_series(
                symbol, interval, split.test_start, split.test_end,
                test_bars, test_funding, test_oi, test_trades_data, mmr_table,
                strat, strat.__class__.__name__, best.params,
                commission_bps=commission_bps, slippage_bps=slippage_bps,
                htf_bars=test_htf_bars,
            )
            oos_trades = oos_result.trades

        folds.append(FoldResult(
            split=split, trials_count=len(param_grid),
            chosen_params=best.params if best is not None else None,
            chosen_survives_bh=chosen_survives, oos_trades=oos_trades,
        ))
        all_oos_trades.extend(oos_trades)

    return WalkForwardReport(
        symbol=symbol, interval=interval,
        # `strategy_key` (el slug snake_case de STRATEGIES, ej.
        # "rsi_pullback_long") si el caller lo pasa -- coincide con
        # `backtest_runs.strategy` y con `StrategyInfo.key` del frontend,
        # asi la vista "Bots" puede resolver el TITULO legible (ver
        # `titleFor` en BotsView.tsx) igual que ya hace con las corridas
        # normales. Sin esto cae al nombre de la clase de Python
        # (`RsiPullbackLong`), que sigue siendo unico e informativo pero
        # no coincide con ninguna key del catalogo.
        strategy_name=strategy_key or strategy_factory.__name__,
        folds=folds, oos_trades=all_oos_trades, total_trials_count=total_trials,
        win_rate=stats_mod.win_rate(all_oos_trades),
        win_rate_ci_95=stats_mod.win_rate_ci(all_oos_trades, seed=0),
        expectancy_r=stats_mod.expectancy_r(all_oos_trades),
        expectancy_r_ci_95=stats_mod.expectancy_r_ci(all_oos_trades, seed=0),
        sample_size=stats_mod.sample_size_flag(all_oos_trades),
        profit_factor=stats_mod.profit_factor(all_oos_trades),
        expectancy_usd=stats_mod.expectancy_usd(all_oos_trades),
        max_drawdown_pct=_max_drawdown_pct(all_oos_trades),
        monthly_usd_per_day=stats_mod.monthly_usd_per_day(all_oos_trades),
        weekly_usd_per_week=stats_mod.weekly_usd_per_week(all_oos_trades),
    )


def _max_drawdown_pct(trades: list[Trade]) -> Optional[float]:
    """Curva de equity de $100 (ver STARTING_CAPITAL_USD) sumando el pnl de
    CADA trade cerrado en orden cronologico por `entry_time_ms` -- se
    reordena aca EXPLICITAMENTE (no asumir que el caller ya viene
    ordenado): un walk-forward de UN simbolo si queda ordenado por
    construccion (folds anchored, nunca se solapan), pero
    `multi_symbol_validation.py` concatena los trades SIMBOLO POR SIMBOLO
    dentro de cada tramo -- sin este sort, la "caida" mezclaria el orden
    real de apertura de cada simbolo con el de otro, una curva de equity
    sin sentido. `None` sin trades cerrados, igual criterio que
    `profit_factor`/`expectancy_usd` de mas arriba."""
    closed = sorted((t for t in trades if t.pnl_usd is not None), key=lambda t: t.entry_time_ms)
    if not closed:
        return None
    equity = [STARTING_CAPITAL_USD]
    running = STARTING_CAPITAL_USD
    for t in closed:
        running += t.pnl_usd
        equity.append(running)
    return stats_mod.max_drawdown(equity)


def verdict(report: WalkForwardReport) -> str:
    """Criterio 1 + 3: cota inferior del IC95% de la expectancy en R > 0 Y
    muestra suficiente (`sample_size == "OK"`, `MIN_TRADES_FOR_REPORTING`
    en statistics.py) -- las dos condiciones son necesarias, ninguna sola
    alcanza. Funcion propia (antes vivia inline en `__main__.py`) porque
    ahora tambien la usa `journal.py` al guardar el reporte -- una sola
    fuente de verdad para el veredicto en vez de dos copias que se puedan
    desalinear.

    Tercera condicion: el IC95% de la expectancy en R puede ser positivo
    mientras el resultado en DOLARES es negativo (`profit_factor < 1`) --
    con SL/TP basados en ATR, el riesgo por trade varia (`risk_usd`
    distinto segun la volatilidad al momento de entrar), asi que el
    promedio de R (normalizado por ese riesgo, peso IGUAL por trade) puede
    divergir del promedio en USD (peso proporcional al riesgo realmente
    tomado) si las perdidas se concentran en los trades de MAYOR riesgo.
    Una estrategia que pierde dinero de verdad no puede considerarse
    aprobada solo porque el R normalizado sea positivo.

    NO incluye `max_drawdown_pct`/`monthly_usd_per_day` como cuarta
    condicion todavia -- son diagnosticos mas exigentes en la practica
    que sostener money real (ver `report_to_dict`), pero agregarlos aca
    cambiaria retroactivamente el criterio de aprobacion sin una decision
    explicita en ese sentido; por ahora se reportan junto al veredicto
    para que la lectura final (¿de verdad haria crecer $100 sin destruir
    la cuenta?) la haga una persona, no esta funcion."""
    return (
        "PASA" if report.expectancy_r_ci_95 is not None and report.expectancy_r_ci_95[0] > 0
        and report.sample_size == "OK"
        and report.profit_factor is not None and report.profit_factor > 1.0
        else "NO PASA"
    )


def report_to_dict(report: WalkForwardReport) -> dict:
    """Serializacion completa de un `WalkForwardReport` a un dict JSON-able
    -- usada tanto por `--validate` (impresion en la CLI) como por
    `journal.save_validation` (persistencia). Un solo lugar que decide que
    forma tiene "un reporte de validacion" en este proyecto."""
    return {
        "symbol": report.symbol, "interval": report.interval,
        "strategy": report.strategy_name, "folds": len(report.folds),
        "total_trials_count": report.total_trials_count,
        "oos_trades": len(report.oos_trades),
        "win_rate": report.win_rate, "win_rate_ci_95": report.win_rate_ci_95,
        "expectancy_r": report.expectancy_r, "expectancy_r_ci_95": report.expectancy_r_ci_95,
        "expectancy_usd": report.expectancy_usd, "profit_factor": report.profit_factor,
        "sample_size": report.sample_size,
        "verdict": verdict(report),
        # Ver STARTING_CAPITAL_USD/DAILY_FLOOR_PCTS arriba: la meta real es
        # crecer $100 con un % diario chico SIN caer en drawdown grave.
        "max_drawdown_pct": report.max_drawdown_pct,
        "monthly_usd_per_day": report.monthly_usd_per_day,
        "consecutive_months_meeting_floor": {
            f"{pct}%": stats_mod.consecutive_months_meeting_floor(
                report.monthly_usd_per_day, STARTING_CAPITAL_USD * pct / 100.0,
            )
            for pct in DAILY_FLOOR_PCTS
        },
        # Meta semanal (ver WEEKLY_FLOOR_PCTS arriba), misma logica que los
        # pisos diarios de arriba pero sobre `weekly_usd_per_week` --
        # ninguno reemplaza al otro, una estrategia de alta frecuencia se
        # lee mejor en diario y una de pocos trades de alta conviccion en
        # semanal.
        "weekly_usd_per_week": report.weekly_usd_per_week,
        "consecutive_weeks_meeting_floor": {
            f"{pct}%": stats_mod.consecutive_weeks_meeting_floor(
                report.weekly_usd_per_week, STARTING_CAPITAL_USD * pct / 100.0,
            )
            for pct in WEEKLY_FLOOR_PCTS
        },
        "by_fold": [
            {
                "train_start": f.split.train_start.isoformat(), "train_end": f.split.train_end.isoformat(),
                "test_start": f.split.test_start.isoformat(), "test_end": f.split.test_end.isoformat(),
                "trials_count": f.trials_count, "chosen_params": f.chosen_params,
                "chosen_survives_bh": f.chosen_survives_bh, "oos_trades": len(f.oos_trades),
            }
            for f in report.folds
        ],
    }
