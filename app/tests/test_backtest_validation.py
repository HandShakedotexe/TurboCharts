"""Sin red/DB: series sinteticas, mismo criterio que el resto del
backtester. Estos tests verifican el ARNES de validacion en si (walk-forward,
trials_count, seleccion por cota inferior de IC), no el resultado de ninguna
estrategia real -- eso se corre y se reporta aparte, contra datos reales."""
import unittest
from datetime import datetime, timedelta, timezone

from backtest.data import Bar
from backtest.engine import TargetPosition, Trade
from backtest.validation import (
    STARTING_CAPITAL_USD,
    SweepResult,
    WalkForwardReport,
    _max_drawdown_pct,
    anchored_walk_forward_splits,
    pick_best,
    report_to_dict,
    sweep_in_sample,
    verdict,
    walk_forward_validate,
)


def _trade(pnl_usd, entry_time_ms=0, trade_seq=1):
    return Trade(trade_seq=trade_seq, side="long", entry_time_ms=entry_time_ms,
                 entry_price=100.0, size_usd=1000.0, signal_snapshot={}, pnl_usd=pnl_usd)


def _report(expectancy_r_ci_95, sample_size="OK", profit_factor=1.5):
    """Reporte minimo para probar `verdict()` en aislamiento -- los campos
    que no le importan a esa funcion (folds, oos_trades, etc.) van vacios/
    cero, no se corre ningun walk-forward real para esto."""
    return WalkForwardReport(
        symbol="TEST", interval="1h", strategy_name="test", folds=[], oos_trades=[],
        total_trials_count=0, win_rate=0.5, win_rate_ci_95=(0.4, 0.6),
        expectancy_r=0.1, expectancy_r_ci_95=expectancy_r_ci_95, sample_size=sample_size,
        profit_factor=profit_factor, expectancy_usd=1.0,
        max_drawdown_pct=0.1, monthly_usd_per_day={}, weekly_usd_per_week={},
    )


class TestMaxDrawdownPct(unittest.TestCase):
    """Corregido 2026-09-19 (malentendido real de esta sesion: la meta NO
    era 50% diario) -- `_max_drawdown_pct` es la pieza que
    responde "¿esto haria crecer $100 sin destruir la cuenta?"."""

    def test_none_without_closed_trades(self):
        self.assertIsNone(_max_drawdown_pct([]))
        self.assertIsNone(_max_drawdown_pct([_trade(None, entry_time_ms=1)]))

    def test_hand_computed_from_100(self):
        # $100 -> +20 (120, nuevo pico) -> -60 (60, caida de 60/120=50%
        # desde el pico) -> +10 (70, todavia por debajo del pico).
        trades = [
            _trade(20.0, entry_time_ms=1), _trade(-60.0, entry_time_ms=2),
            _trade(10.0, entry_time_ms=3),
        ]
        self.assertAlmostEqual(_max_drawdown_pct(trades), 0.5)

    def test_sorts_by_entry_time_regardless_of_input_order(self):
        """Mismos trades que arriba, pasados FUERA de orden -- confirma que
        se reordenan por `entry_time_ms` antes de armar la curva de
        equity (necesario para multi_symbol_validation.py, que concatena
        simbolo por simbolo dentro de cada tramo, no cronologicamente)."""
        trades = [
            _trade(10.0, entry_time_ms=3), _trade(20.0, entry_time_ms=1),
            _trade(-60.0, entry_time_ms=2),
        ]
        self.assertAlmostEqual(_max_drawdown_pct(trades), 0.5)


class TestReportToDictDailyFloors(unittest.TestCase):
    def test_exposes_max_drawdown_and_consecutive_months_per_floor(self):
        report = _report((0.01, 0.5), "OK", 1.2)
        report.max_drawdown_pct = 0.35
        report.monthly_usd_per_day = {"2026-01": 5.0, "2026-02": 0.2}
        d = report_to_dict(report)
        self.assertAlmostEqual(d["max_drawdown_pct"], 0.35)
        self.assertEqual(d["monthly_usd_per_day"], {"2026-01": 5.0, "2026-02": 0.2})
        # 100 * 0.5% = $0.50/dia: enero (5.0) lo cumple, febrero (0.2) no
        # -- racha de 1 mes. 100 * 3% = $3/dia: solo enero lo cumple
        # tambien -- racha de 1 mes igual (no 2, porque los umbrales mas
        # altos no "suman" meses que ya fallaban al piso mas bajo).
        self.assertEqual(d["consecutive_months_meeting_floor"]["0.5%"], 1)
        self.assertEqual(d["consecutive_months_meeting_floor"]["3.0%"], 1)


class TestReportToDictWeeklyFloors(unittest.TestCase):
    def test_exposes_weekly_usd_and_consecutive_weeks_per_floor(self):
        report = _report((0.01, 0.5), "OK", 1.2)
        report.weekly_usd_per_week = {"2026-W01": 5.0, "2026-W02": 5.0, "2026-W03": 0.2}
        d = report_to_dict(report)
        self.assertEqual(d["weekly_usd_per_week"], {"2026-W01": 5.0, "2026-W02": 5.0, "2026-W03": 0.2})
        # 100 * 2% = $2/semana: las dos primeras semanas lo cumplen (racha
        # de 2), la tercera no -- 100 * 3% = $3/semana, mismo resultado.
        self.assertEqual(d["consecutive_weeks_meeting_floor"]["2.0%"], 2)
        self.assertEqual(d["consecutive_weeks_meeting_floor"]["3.0%"], 2)


class TestVerdict(unittest.TestCase):
    """El IC95% de la expectancy en R puede ser positivo con
    profit_factor < 1 (perdida real en dolares) -- `verdict`
    exige las dos cosas, no solo el IC en R."""

    def test_passes_when_r_ci_positive_sample_ok_and_profit_factor_above_one(self):
        self.assertEqual(verdict(_report((0.01, 0.5), "OK", 1.2)), "PASA")

    def test_fails_when_profit_factor_at_or_below_one_even_if_r_ci_positive(self):
        # el caso real que motivo el cambio: R positivo, plata negativa.
        self.assertEqual(verdict(_report((0.001, 0.05), "OK", 0.99)), "NO PASA")
        self.assertEqual(verdict(_report((0.01, 0.5), "OK", 1.0)), "NO PASA")

    def test_fails_when_profit_factor_is_none(self):
        self.assertEqual(verdict(_report((0.01, 0.5), "OK", None)), "NO PASA")

    def test_fails_when_r_ci_crosses_zero_regardless_of_profit_factor(self):
        self.assertEqual(verdict(_report((-0.01, 0.5), "OK", 2.0)), "NO PASA")

    def test_fails_when_sample_insufficient_regardless_of_the_rest(self):
        self.assertEqual(verdict(_report((0.01, 0.5), "INSUFFICIENT_SAMPLE", 2.0)), "NO PASA")

    def test_fails_when_no_ci_at_all(self):
        self.assertEqual(verdict(_report(None, "OK", 2.0)), "NO PASA")


def _hourly_bars(start: datetime, n: int, price: float = 100.0) -> list[Bar]:
    """Precio CONSTANTE a proposito: sin movimiento, ningun SL/TP se toca
    nunca y las posiciones se cierran solo al final de cada ventana (fin de
    corrida) -- predecible para afirmar EXACTAMENTE cuantos trials/folds
    corrieron, que es lo que estos tests verifican, no el resultado
    economico de una estrategia real."""
    start_ms = int(start.timestamp() * 1000)
    return [
        Bar(start_ms + i * 3_600_000, price, price + 1, price - 1, price, 10.0, 5.0)
        for i in range(n)
    ]


class AlwaysLongStrategy:
    """Entra largo en la primera barra donde esta plana, con un SL/TP fijo
    por parametro -- una estrategia sintetica minima para poder afirmar
    EXACTAMENTE cuantos trials corrio un barrido."""

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


class TestAnchoredWalkForwardSplits(unittest.TestCase):
    def test_train_window_anchors_at_start_and_grows(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 21, tzinfo=timezone.utc)  # 20 dias
        splits = anchored_walk_forward_splits(start, end, min_train_days=5, test_days=5)
        self.assertEqual(len(splits), 3)  # [5,10) [10,15) [15,20)
        self.assertEqual(splits[0].train_start, start)
        self.assertEqual(splits[0].train_end, start + timedelta(days=5))
        # el segundo tramo entrena con MAS historia que el primero (ancla en start, crece)
        self.assertEqual(splits[1].train_end, start + timedelta(days=10))
        self.assertEqual(splits[1].train_start, start)

    def test_test_windows_never_overlap(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 21, tzinfo=timezone.utc)
        splits = anchored_walk_forward_splits(start, end, min_train_days=5, test_days=5)
        for a, b in zip(splits, splits[1:]):
            self.assertLessEqual(a.test_end, b.test_start)

    def test_not_enough_data_for_a_single_fold_yields_no_splits(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 5, tzinfo=timezone.utc)  # 4 dias, menos que min_train_days
        self.assertEqual(anchored_walk_forward_splits(start, end, min_train_days=5, test_days=5), [])


class TestSweepInSample(unittest.TestCase):
    def test_trials_count_equals_the_grid_size_exactly(self):
        """El punto entero de contar ANTES de saber cual gana -- ver
        docstring de sweep_in_sample."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = _hourly_bars(start, 48)
        grid = [{"stop_pct": p} for p in [0.01, 0.02, 0.03, 0.04, 0.05]]
        results = sweep_in_sample(
            AlwaysLongStrategy, grid, "TEST", "1h",
            bars, [], [], [], None, start, start + timedelta(hours=48),
            commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(len(results), 5)
        self.assertEqual({tuple(r.params.items()) for r in results}, {tuple(p.items()) for p in grid})


class TestPickBest(unittest.TestCase):
    def test_picks_the_highest_ci_lower_bound_not_the_highest_point_estimate(self):
        """Una expectancy alta pero con un CI que se hunde en negativo
        pierde contra una mas modesta pero mas segura -- el criterio es
        la COTA INFERIOR, no el punto medio."""
        risky = SweepResult(params={"k": "risky"}, trades_count=3,
                             expectancy_r=5.0, expectancy_r_ci_95=(-2.0, 12.0), p_value=0.3)
        safe = SweepResult(params={"k": "safe"}, trades_count=50,
                            expectancy_r=0.8, expectancy_r_ci_95=(0.3, 1.3), p_value=0.01)
        self.assertEqual(pick_best([risky, safe]).params, {"k": "safe"})

    def test_ignores_configs_with_no_trades_in_the_training_window(self):
        empty = SweepResult(params={"k": "empty"}, trades_count=0,
                             expectancy_r=None, expectancy_r_ci_95=None, p_value=None)
        real = SweepResult(params={"k": "real"}, trades_count=10,
                            expectancy_r=0.5, expectancy_r_ci_95=(0.1, 0.9), p_value=0.02)
        self.assertEqual(pick_best([empty, real]).params, {"k": "real"})

    def test_no_candidates_at_all_returns_none(self):
        empty = SweepResult(params={}, trades_count=0, expectancy_r=None,
                             expectancy_r_ci_95=None, p_value=None)
        self.assertIsNone(pick_best([empty]))


class TestWalkForwardValidate(unittest.TestCase):
    def test_total_trials_count_is_folds_times_grid_size(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        bars = _hourly_bars(start, 20 * 24)
        grid = [{"stop_pct": p} for p in [0.02, 0.05]]
        report = walk_forward_validate(
            AlwaysLongStrategy, grid, "TEST", "1h",
            bars, [], [], [], None, start, end,
            min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(len(report.folds), 3)
        self.assertEqual(report.total_trials_count, 3 * 2)

    def test_out_of_sample_trades_only_fall_inside_their_own_test_window(self):
        """La condicion NECESARIA del criterio 3: ningun trade
        out-of-sample puede venir de datos que tambien se usaron para
        elegir esos parametros."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=20)
        bars = _hourly_bars(start, 20 * 24)
        grid = [{"stop_pct": 0.02}]
        report = walk_forward_validate(
            AlwaysLongStrategy, grid, "TEST", "1h",
            bars, [], [], [], None, start, end,
            min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        for fold in report.folds:
            test_start_ms = int(fold.split.test_start.timestamp() * 1000)
            test_end_ms = int(fold.split.test_end.timestamp() * 1000)
            for t in fold.oos_trades:
                self.assertGreaterEqual(t.entry_time_ms, test_start_ms)
                self.assertLess(t.entry_time_ms, test_end_ms)

    def test_empty_data_range_yields_an_insufficient_sample_report_not_a_crash(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=2)   # menos que min_train_days: cero folds
        report = walk_forward_validate(
            AlwaysLongStrategy, [{"stop_pct": 0.02}], "TEST", "1h",
            [], [], [], [], None, start, end,
            min_train_days=5, test_days=5, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(report.folds, [])
        self.assertEqual(report.sample_size, "INSUFFICIENT_SAMPLE")
        self.assertIsNone(report.expectancy_r)


if __name__ == "__main__":
    unittest.main()
