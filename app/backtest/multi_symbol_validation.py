"""Walk-forward multi-simbolo -- la pieza que faltaba frente a dos
enfoques parciales: `stage2.py` poolea simbolos pero SIN split train/test
(in-sample sobre todo el historico, que puede dar un resultado optimista
frente a un walk-forward real de un solo simbolo); `validation.walk_forward_validate`
hace walk-forward real pero UN SOLO simbolo a la vez, dejando la pregunta
"¿la muestra chica de un simbolo esconde un edge real que pool si
mostraria?" sin responder con el rigor completo.

Este modulo junta las dos cosas: walk-forward anchored (mismos splits que
`validation.anchored_walk_forward_splits`) donde CADA tramo -- entrenamiento
Y prueba -- corre sobre TODOS los simbolos a la vez, con los trades
pooled ANTES de calcular el IC/p-valor de cada configuracion. Reusa
`SweepResult`/`FoldResult`/`WalkForwardReport`/`pick_best`/
`anchored_walk_forward_splits`/`_slice_by_time` de `validation.py` tal
cual -- no una segunda implementacion, la misma logica de eleccion y
reporte, solo con datos de mas de un simbolo por tramo."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import statistics as stats_mod
from .engine import Strategy, Trade, run_backtest_on_series
from .validation import (
    FoldResult,
    SweepResult,
    WalkForwardReport,
    WalkForwardSplit,
    _max_drawdown_pct,
    _slice_by_time,
    anchored_walk_forward_splits,
    pick_best,
)


def sweep_in_sample_pooled(
    strategy_factory: Callable[..., Strategy],
    param_grid: list[dict],
    symbols: list[str], interval: str,
    data_by_symbol: dict[str, dict],
    train_start: datetime, train_end: datetime,
    commission_bps: float, slippage_bps: float,
) -> list[SweepResult]:
    """Como `validation.sweep_in_sample`, pero cada configuracion se corre
    en TODOS los simbolos y los trades se JUNTAN antes de medir el IC/
    p-valor -- una sola cifra de "que tan bien le fue a esta config" sobre
    el pool entero, no un promedio de cifras por simbolo."""
    out = []
    for params in param_grid:
        all_trades: list[Trade] = []
        for symbol in symbols:
            d = data_by_symbol[symbol]
            strat = strategy_factory(**params)
            result = run_backtest_on_series(
                symbol, interval, train_start, train_end,
                d["bars"], d["funding"], d["oi"], d["big_trades"], d["mmr_table"],
                strat, strat.__class__.__name__, params,
                commission_bps=commission_bps, slippage_bps=slippage_bps,
            )
            all_trades.extend(result.trades)
        exp_r = stats_mod.expectancy_r(all_trades)
        ci = stats_mod.expectancy_r_ci(all_trades, seed=0)
        p = stats_mod.expectancy_r_p_value(all_trades, seed=0)
        closed = sum(1 for t in all_trades if t.pnl_usd is not None)
        out.append(SweepResult(params, closed, exp_r, ci, p))
    return out


def _checkpoint_config(
    strategy_name: str, param_grid: list[dict], symbols: list[str], interval: str,
    start: datetime, end: datetime, min_train_days: int, test_days: int,
    commission_bps: float, slippage_bps: float, bh_alpha: float,
) -> dict:
    return json.loads(json.dumps({
        "strategy": strategy_name, "param_grid": param_grid, "symbols": symbols,
        "interval": interval, "start": start.isoformat(), "end": end.isoformat(),
        "min_train_days": min_train_days, "test_days": test_days,
        "commission_bps": commission_bps, "slippage_bps": slippage_bps, "bh_alpha": bh_alpha,
    }))


def _fold_to_dict(fold: FoldResult) -> dict:
    return {
        "split": {
            "train_start": fold.split.train_start.isoformat(),
            "train_end": fold.split.train_end.isoformat(),
            "test_start": fold.split.test_start.isoformat(),
            "test_end": fold.split.test_end.isoformat(),
        },
        "trials_count": fold.trials_count,
        "chosen_params": fold.chosen_params,
        "chosen_survives_bh": fold.chosen_survives_bh,
        "oos_trades": [asdict(t) for t in fold.oos_trades],
    }


def _fold_from_dict(d: dict) -> FoldResult:
    s = d["split"]
    return FoldResult(
        split=WalkForwardSplit(
            datetime.fromisoformat(s["train_start"]), datetime.fromisoformat(s["train_end"]),
            datetime.fromisoformat(s["test_start"]), datetime.fromisoformat(s["test_end"]),
        ),
        trials_count=d["trials_count"],
        chosen_params=d["chosen_params"],
        chosen_survives_bh=d["chosen_survives_bh"],
        oos_trades=[Trade(**t) for t in d["oos_trades"]],
    )


def _load_checkpoint(path: str, config: dict) -> list[FoldResult]:
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if data["config"] != config:
        raise ValueError(
            f"el checkpoint {path} es de OTRA configuracion (estrategia, simbolos, "
            "fechas o grilla distintos) -- usa otra ruta o borralo para empezar de cero"
        )
    return [_fold_from_dict(d) for d in data["folds"]]


def _save_checkpoint(path: str, config: dict, folds: list[FoldResult]) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps({"config": config, "folds": [_fold_to_dict(f) for f in folds]}),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def multi_symbol_walk_forward_validate(
    strategy_factory: Callable[..., Strategy],
    param_grid: list[dict],
    symbols: list[str], interval: str,
    data_by_symbol: dict[str, dict],
    start: datetime, end: datetime,
    min_train_days: int, test_days: int,
    commission_bps: float = 5.0, slippage_bps: float = 2.0,
    bh_alpha: float = 0.05,
    strategy_key: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
) -> WalkForwardReport:
    """`data_by_symbol[symbol]` = {"bars", "funding", "oi", "big_trades",
    "mmr_table"} -- series COMPLETAS (rango entero, ya cargadas una vez
    por el caller), rebanadas aqui por tramo igual que hace
    `validation.walk_forward_validate` para un solo simbolo."""
    splits = anchored_walk_forward_splits(start, end, min_train_days, test_days)
    config = None
    folds: list[FoldResult] = []
    if checkpoint_path:
        config = _checkpoint_config(
            strategy_key or strategy_factory.__name__, param_grid, symbols, interval,
            start, end, min_train_days, test_days, commission_bps, slippage_bps, bh_alpha,
        )
        folds = _load_checkpoint(checkpoint_path, config)
    all_oos_trades: list[Trade] = [t for f in folds for t in f.oos_trades]
    total_trials = sum(f.trials_count for f in folds)

    for split in splits[len(folds):]:
        train_start_ms = int(split.train_start.timestamp() * 1000)
        train_end_ms = int(split.train_end.timestamp() * 1000)
        test_start_ms = int(split.test_start.timestamp() * 1000)
        test_end_ms = int(split.test_end.timestamp() * 1000)

        train_data_by_symbol = {
            symbol: {
                "bars": _slice_by_time(d["bars"], train_start_ms, train_end_ms),
                "funding": _slice_by_time(d["funding"], train_start_ms, train_end_ms),
                "oi": _slice_by_time(d["oi"], train_start_ms, train_end_ms),
                "big_trades": _slice_by_time(d["big_trades"], train_start_ms, train_end_ms),
                "mmr_table": d["mmr_table"],
            }
            for symbol, d in data_by_symbol.items()
        }
        sweep = sweep_in_sample_pooled(
            strategy_factory, param_grid, symbols, interval, train_data_by_symbol,
            split.train_start, split.train_end, commission_bps, slippage_bps,
        )
        total_trials += len(param_grid)
        best = pick_best(sweep)
        survivors = stats_mod.benjamini_hochberg([s.p_value for s in sweep], alpha=bh_alpha)
        best_idx = sweep.index(best) if best is not None else -1
        chosen_survives = best_idx >= 0 and survivors[best_idx]

        oos_trades: list[Trade] = []
        if best is not None:
            for symbol, d in data_by_symbol.items():
                test_bars = _slice_by_time(d["bars"], test_start_ms, test_end_ms)
                test_funding = _slice_by_time(d["funding"], test_start_ms, test_end_ms)
                test_oi = _slice_by_time(d["oi"], test_start_ms, test_end_ms)
                test_big_trades = _slice_by_time(d["big_trades"], test_start_ms, test_end_ms)
                strat = strategy_factory(**best.params)
                oos_result = run_backtest_on_series(
                    symbol, interval, split.test_start, split.test_end,
                    test_bars, test_funding, test_oi, test_big_trades, d["mmr_table"],
                    strat, strat.__class__.__name__, best.params,
                    commission_bps=commission_bps, slippage_bps=slippage_bps,
                )
                oos_trades.extend(oos_result.trades)

        folds.append(FoldResult(
            split=split, trials_count=len(param_grid),
            chosen_params=best.params if best is not None else None,
            chosen_survives_bh=chosen_survives, oos_trades=oos_trades,
        ))
        all_oos_trades.extend(oos_trades)
        if checkpoint_path:
            _save_checkpoint(checkpoint_path, config, folds)

    return WalkForwardReport(
        # `symbol` es un campo de UN simbolo en el dataclass original --
        # se reusa con la lista unida por comas para no bifurcar el
        # reporte en un tipo nuevo solo por esto; `journal.py`/el frontend
        # lo muestran tal cual, como un identificador, no lo parsean.
        symbol=",".join(symbols), interval=interval,
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
