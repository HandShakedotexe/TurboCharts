"""Sin red/DB: series sinteticas para DOS simbolos, mismo criterio que
test_backtest_validation.py -- verifica que el POOLEO en si funciona
(trades de los dos simbolos se juntan, ningun trade cruza su ventana),
no el resultado de ninguna estrategia real."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backtest.data import Bar
from backtest.engine import TargetPosition
from backtest.multi_symbol_validation import (
    multi_symbol_walk_forward_validate,
    sweep_in_sample_pooled,
)
from backtest.validation import SweepResult


def _hourly_bars(start: datetime, n: int, price: float = 100.0) -> list[Bar]:
    start_ms = int(start.timestamp() * 1000)
    return [
        Bar(start_ms + i * 3_600_000, price, price + 1, price - 1, price, 10.0, 5.0)
        for i in range(n)
    ]


class AlwaysLongStrategy:
    def __init__(self, stop_pct: float = 0.02, target_pct: float = 0.02, size_usd: float = 1000.0):
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.size_usd = size_usd

    def on_bar(self, ctx):
        if ctx.position.side == "flat":
            price = ctx.bar.close
            return TargetPosition(
                "long", self.size_usd,
                stop_loss_price=price * (1 - self.stop_pct),
                take_profit_price=price * (1 + self.target_pct),
            )
        return None


def _two_symbol_data(start: datetime, n: int) -> dict[str, dict]:
    return {
        "AAAUSDT": {"bars": _hourly_bars(start, n), "funding": [], "oi": [], "big_trades": [], "mmr_table": None},
        "BBBUSDT": {"bars": _hourly_bars(start, n), "funding": [], "oi": [], "big_trades": [], "mmr_table": None},
    }


class TestSweepInSamplePooled(unittest.TestCase):
    def test_trades_count_sums_across_symbols(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=5)
        data = _two_symbol_data(start, 5 * 24)
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}]
        sweep = sweep_in_sample_pooled(
            AlwaysLongStrategy, grid, ["AAAUSDT", "BBBUSDT"], "1h", data,
            start, end, commission_bps=0.0, slippage_bps=0.0,
        )
        # precio constante en las dos: nunca toca SL/TP, se cierra 1 vez
        # al final de la ventana en CADA simbolo -- 1+1 = 2 trades pooled.
        self.assertEqual(sweep[0].trades_count, 2)


class TestMultiSymbolWalkForwardValidate(unittest.TestCase):
    def test_total_trials_count_is_folds_times_grid_size(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        data = _two_symbol_data(start, 20 * 24)
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}, {"stop_pct": 0.05, "target_pct": 0.05}]
        report = multi_symbol_walk_forward_validate(
            AlwaysLongStrategy, grid, ["AAAUSDT", "BBBUSDT"], "1h", data,
            start, end, min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(len(report.folds), 3)
        self.assertEqual(report.total_trials_count, 3 * 2)

    def test_oos_trades_come_from_both_symbols(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        data = _two_symbol_data(start, 20 * 24)
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}]
        report = multi_symbol_walk_forward_validate(
            AlwaysLongStrategy, grid, ["AAAUSDT", "BBBUSDT"], "1h", data,
            start, end, min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        # cada tramo de prueba (5 dias, precio plano) cierra 1 trade por
        # simbolo al final -- 2 por tramo, 3 tramos = 6.
        self.assertEqual(len(report.oos_trades), 6)

    def test_no_symbol_data_leaks_across_fold_boundaries(self):
        """Mismo chequeo que test_backtest_validation.py de un simbolo,
        aplicado a los dos: ningun trade OOS puede originarse fuera de la
        ventana de prueba de su propio tramo."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        data = _two_symbol_data(start, 20 * 24)
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}]
        report = multi_symbol_walk_forward_validate(
            AlwaysLongStrategy, grid, ["AAAUSDT", "BBBUSDT"], "1h", data,
            start, end, min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        for fold in report.folds:
            test_start_ms = int(fold.split.test_start.timestamp() * 1000)
            test_end_ms = int(fold.split.test_end.timestamp() * 1000)
            for t in fold.oos_trades:
                self.assertGreaterEqual(t.entry_time_ms, test_start_ms)
                self.assertLess(t.entry_time_ms, test_end_ms)


class TestCheckpoint(unittest.TestCase):
    """Un walk-forward de dias de computo no puede empezar de cero tras un
    corte (2026-10-03: dos dias de cómputo perdidos en un solo reinicio)."""

    def _run(self, path: str, grid: list[dict]):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        data = _two_symbol_data(start, 20 * 24)
        return multi_symbol_walk_forward_validate(
            AlwaysLongStrategy, grid, ["AAAUSDT", "BBBUSDT"], "1h", data,
            start, end, min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
            checkpoint_path=path,
        )

    def test_resume_from_partial_checkpoint_matches_full_run(self):
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cp.json")
            full = self._run(path, grid)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["folds"] = data["folds"][:1]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            resumed = self._run(path, grid)
        self.assertEqual(len(resumed.folds), 3)
        self.assertEqual(resumed.oos_trades, full.oos_trades)
        self.assertEqual(resumed.total_trials_count, full.total_trials_count)
        self.assertEqual(resumed.expectancy_r, full.expectancy_r)

    def test_checkpoint_from_other_config_is_rejected(self):
        grid = [{"stop_pct": 0.02, "target_pct": 0.02}]
        other = [{"stop_pct": 0.05, "target_pct": 0.05}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cp.json")
            self._run(path, grid)
            with self.assertRaises(ValueError):
                self._run(path, other)


if __name__ == "__main__":
    unittest.main()
