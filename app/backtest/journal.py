"""Trade journal auditable -- persistencia fina, sin logica.
`backtest_runs`/`backtest_trades` en app/db/schema.sql. `run_id` se genera
aqui con `uuid.uuid4().hex` (no `gen_random_uuid()` de Postgres, para no
asumir la extension pgcrypto -- ver el comentario de la tabla en schema.sql).
"""
from __future__ import annotations

import uuid

from .engine import BacktestResult
from .validation import WalkForwardReport


def _epoch_ms_to_dt(ms: int):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def save_run(conn, result: BacktestResult) -> str:
    from psycopg.types.json import Jsonb

    run_id = uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO backtest_runs
               (run_id, symbol, interval, strategy, params, data_start, data_end,
                commission_bps, slippage_bps)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                run_id, result.symbol, result.interval, result.strategy_name,
                Jsonb(result.params), result.data_start, result.data_end,
                result.commission_bps, result.slippage_bps,
            ),
        )
        for trade in result.trades:
            cur.execute(
                """INSERT INTO backtest_trades
                   (run_id, trade_seq, side, entry_time, entry_price, exit_time, exit_price,
                    size_usd, commission_usd, funding_paid_usd, pnl_usd, signal_snapshot,
                    slippage_usd, mfe_usd, mae_usd, exit_signal_snapshot, exit_reason,
                    stop_loss_price, take_profit_price, risk_usd, r_multiple)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s)""",
                (
                    run_id, trade.trade_seq, trade.side,
                    _epoch_ms_to_dt(trade.entry_time_ms), trade.entry_price,
                    _epoch_ms_to_dt(trade.exit_time_ms) if trade.exit_time_ms is not None else None,
                    trade.exit_price, trade.size_usd, trade.commission_usd,
                    trade.funding_paid_usd, trade.pnl_usd, Jsonb(trade.signal_snapshot),
                    trade.slippage_usd, trade.mfe_usd, trade.mae_usd,
                    Jsonb(trade.exit_signal_snapshot) if trade.exit_signal_snapshot is not None else None,
                    trade.exit_reason,
                    trade.stop_loss_price, trade.take_profit_price, trade.risk_usd, trade.r_multiple,
                ),
            )
        for seq, (time_ms, equity) in enumerate(result.equity_curve):
            cur.execute(
                """INSERT INTO backtest_equity_curve (run_id, seq, time, equity)
                   VALUES (%s, %s, %s, %s)""",
                (run_id, seq, _epoch_ms_to_dt(time_ms), equity),
            )
    conn.commit()
    return run_id


def load_run(conn, run_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_id, started_at, symbol, interval, strategy, params,
                      data_start, data_end, commission_bps, slippage_bps
               FROM backtest_runs WHERE run_id=%s""",
            (run_id,),
        )
        run_row = cur.fetchone()
        if run_row is None:
            raise KeyError(f"no existe backtest_runs.run_id={run_id!r}")
        cur.execute(
            """SELECT trade_seq, side, entry_time, entry_price, exit_time, exit_price,
                      size_usd, commission_usd, funding_paid_usd, pnl_usd, signal_snapshot,
                      slippage_usd, mfe_usd, mae_usd, exit_signal_snapshot, exit_reason,
                      stop_loss_price, take_profit_price, risk_usd, r_multiple
               FROM backtest_trades WHERE run_id=%s ORDER BY trade_seq""",
            (run_id,),
        )
        trade_rows = cur.fetchall()
        cur.execute(
            """SELECT time, equity FROM backtest_equity_curve
               WHERE run_id=%s ORDER BY seq""",
            (run_id,),
        )
        equity_rows = cur.fetchall()
    columns = ["run_id", "started_at", "symbol", "interval", "strategy", "params",
               "data_start", "data_end", "commission_bps", "slippage_bps"]
    run = dict(zip(columns, run_row))
    trade_columns = ["trade_seq", "side", "entry_time", "entry_price", "exit_time", "exit_price",
                      "size_usd", "commission_usd", "funding_paid_usd", "pnl_usd", "signal_snapshot",
                      "slippage_usd", "mfe_usd", "mae_usd", "exit_signal_snapshot", "exit_reason",
                      "stop_loss_price", "take_profit_price", "risk_usd", "r_multiple"]
    run["trades"] = [dict(zip(trade_columns, row)) for row in trade_rows]
    run["equity_curve"] = [{"time": t, "equity": e} for t, e in equity_rows]
    return run


def save_validation(conn, report: WalkForwardReport, min_train_days: int, test_days: int) -> str:
    """Persiste un `WalkForwardReport` completo -- ver `backtest_validations`
    en db/schema.sql sobre por que es una tabla propia y no una fila mas de
    `backtest_runs`. `by_fold` (JSONB) sale de `validation.report_to_dict`,
    la MISMA funcion que ya usa `--validate` para imprimir por consola --
    una sola forma de serializar un reporte, no dos que se puedan
    desalinear."""
    from psycopg.types.json import Jsonb

    from .validation import report_to_dict, verdict

    validation_id = uuid.uuid4().hex
    d = report_to_dict(report)
    win_ci = report.win_rate_ci_95
    exp_ci = report.expectancy_r_ci_95
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO backtest_validations
               (validation_id, symbol, interval, strategy, data_start, data_end,
                min_train_days, test_days, folds, total_trials_count, oos_trades,
                win_rate, win_rate_ci_low, win_rate_ci_high,
                expectancy_r, expectancy_r_ci_low, expectancy_r_ci_high,
                profit_factor, expectancy_usd,
                max_drawdown_pct, monthly_usd_per_day, weekly_usd_per_week,
                sample_size, verdict, by_fold)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                validation_id, report.symbol, report.interval, report.strategy_name,
                report.folds[0].split.train_start if report.folds else None,
                report.folds[-1].split.test_end if report.folds else None,
                min_train_days, test_days, len(report.folds), report.total_trials_count,
                len(report.oos_trades), report.win_rate, win_ci[0], win_ci[1],
                report.expectancy_r,
                exp_ci[0] if exp_ci is not None else None, exp_ci[1] if exp_ci is not None else None,
                report.profit_factor, report.expectancy_usd,
                report.max_drawdown_pct, Jsonb(report.monthly_usd_per_day), Jsonb(report.weekly_usd_per_week),
                report.sample_size, verdict(report), Jsonb(d["by_fold"]),
            ),
        )
        _save_validation_trades(cur, validation_id, report)
    conn.commit()
    return validation_id


def _save_validation_trades(cur, validation_id: str, report: WalkForwardReport) -> None:
    """Los trades OOS reales de CADA tramo (no solo el conteo que ya lleva
    `by_fold`) -- ver `backtest_validation_trades` en db/schema.sql. Mismas
    columnas/orden que `save_run` usa para `backtest_trades`, para que
    `load_validation_trades` devuelva algo con la MISMA forma que
    `load_run` y el frontend pueda reusar el mismo componente de tabla."""
    from psycopg.types.json import Jsonb

    for fold_index, fold in enumerate(report.folds):
        # `trade_seq` de aca en mas es el INDICE dentro de `oos_trades` de
        # este tramo, no `trade.trade_seq` -- ese es un contador propio del
        # motor que arranca en 1 por CADA corrida de `run_backtest_on_series`
        # (ver engine.py). Un `WalkForwardReport` de un solo simbolo tiene
        # una sola corrida por tramo, asi que coincidian por accidente; un
        # reporte MULTI-SIMBOLO (`multi_symbol_validation.py`) junta los
        # trades de VARIAS corridas (una por simbolo) en el mismo tramo, y
        # dos simbolos repiten `trade_seq=1,2,3...` cada uno desde cero --
        # violaba la clave primaria `(validation_id, fold_index, trade_seq)`
        # en cuanto el segundo simbolo con trades entraba al mismo tramo.
        # El indice de enumeracion es unico por construccion sin importar
        # cuantas corridas se hayan mezclado.
        for trade_seq, trade in enumerate(fold.oos_trades):
            cur.execute(
                """INSERT INTO backtest_validation_trades
                   (validation_id, fold_index, trade_seq, side, entry_time, entry_price,
                    exit_time, exit_price, size_usd, commission_usd, funding_paid_usd,
                    pnl_usd, signal_snapshot, slippage_usd, mfe_usd, mae_usd,
                    exit_signal_snapshot, exit_reason, stop_loss_price, take_profit_price,
                    risk_usd, r_multiple)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s)""",
                (
                    validation_id, fold_index, trade_seq, trade.side,
                    _epoch_ms_to_dt(trade.entry_time_ms), trade.entry_price,
                    _epoch_ms_to_dt(trade.exit_time_ms) if trade.exit_time_ms is not None else None,
                    trade.exit_price, trade.size_usd, trade.commission_usd,
                    trade.funding_paid_usd, trade.pnl_usd, Jsonb(trade.signal_snapshot),
                    trade.slippage_usd, trade.mfe_usd, trade.mae_usd,
                    Jsonb(trade.exit_signal_snapshot) if trade.exit_signal_snapshot is not None else None,
                    trade.exit_reason,
                    trade.stop_loss_price, trade.take_profit_price, trade.risk_usd, trade.r_multiple,
                ),
            )


def load_validations(conn) -> list[dict]:
    """Resumen de TODAS las validaciones guardadas, mas nuevas primero --
    para la lista en la vista "Bots" (sin `by_fold`, pesado y no hace
    falta para una fila de tabla)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT validation_id, started_at, symbol, interval, strategy,
                      data_start, data_end, min_train_days, test_days, folds,
                      total_trials_count, oos_trades, win_rate, win_rate_ci_low,
                      win_rate_ci_high, expectancy_r, expectancy_r_ci_low,
                      expectancy_r_ci_high, profit_factor, expectancy_usd,
                      max_drawdown_pct, monthly_usd_per_day, weekly_usd_per_week,
                      sample_size, verdict
               FROM backtest_validations ORDER BY started_at DESC""",
        )
        rows = cur.fetchall()
    columns = ["validation_id", "started_at", "symbol", "interval", "strategy",
               "data_start", "data_end", "min_train_days", "test_days", "folds",
               "total_trials_count", "oos_trades", "win_rate", "win_rate_ci_low",
               "win_rate_ci_high", "expectancy_r", "expectancy_r_ci_low",
               "expectancy_r_ci_high", "profit_factor", "expectancy_usd",
               "max_drawdown_pct", "monthly_usd_per_day", "weekly_usd_per_week",
               "sample_size", "verdict"]
    return [dict(zip(columns, row)) for row in rows]


def load_validation(conn, validation_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT validation_id, started_at, symbol, interval, strategy,
                      data_start, data_end, min_train_days, test_days, folds,
                      total_trials_count, oos_trades, win_rate, win_rate_ci_low,
                      win_rate_ci_high, expectancy_r, expectancy_r_ci_low,
                      expectancy_r_ci_high, profit_factor, expectancy_usd,
                      max_drawdown_pct, monthly_usd_per_day, weekly_usd_per_week,
                      sample_size, verdict, by_fold
               FROM backtest_validations WHERE validation_id=%s""",
            (validation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"no existe backtest_validations.validation_id={validation_id!r}")
    columns = ["validation_id", "started_at", "symbol", "interval", "strategy",
               "data_start", "data_end", "min_train_days", "test_days", "folds",
               "total_trials_count", "oos_trades", "win_rate", "win_rate_ci_low",
               "win_rate_ci_high", "expectancy_r", "expectancy_r_ci_low",
               "expectancy_r_ci_high", "profit_factor", "expectancy_usd",
               "max_drawdown_pct", "monthly_usd_per_day", "weekly_usd_per_week",
               "sample_size", "verdict", "by_fold"]
    return dict(zip(columns, row))


def load_validation_trades(conn, validation_id: str) -> list[dict]:
    """Trades OOS reales de una validacion, todos los tramos juntos y
    ordenados por tiempo de entrada -- MISMA forma de columnas que
    `load_run` devuelve para `backtest_trades` (mismo orden que `save_run`
    usa), para que el frontend reuse el mismo componente de tabla de
    trades sin traducir un shape distinto."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT trade_seq, side, entry_time, entry_price, exit_time, exit_price,
                      size_usd, commission_usd, funding_paid_usd, pnl_usd, signal_snapshot,
                      slippage_usd, mfe_usd, mae_usd, exit_signal_snapshot, exit_reason,
                      stop_loss_price, take_profit_price, risk_usd, r_multiple
               FROM backtest_validation_trades WHERE validation_id=%s
               ORDER BY entry_time, fold_index, trade_seq""",
            (validation_id,),
        )
        rows = cur.fetchall()
    columns = ["trade_seq", "side", "entry_time", "entry_price", "exit_time", "exit_price",
               "size_usd", "commission_usd", "funding_paid_usd", "pnl_usd", "signal_snapshot",
               "slippage_usd", "mfe_usd", "mae_usd", "exit_signal_snapshot", "exit_reason",
               "stop_loss_price", "take_profit_price", "risk_usd", "r_multiple"]
    return [dict(zip(columns, row)) for row in rows]
