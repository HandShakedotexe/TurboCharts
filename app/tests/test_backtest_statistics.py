import unittest
from datetime import datetime, timezone

from backtest.engine import Trade
from backtest.statistics import (
    bootstrap_ci,
    benjamini_hochberg,
    consecutive_months_meeting_floor,
    consecutive_weeks_meeting_floor,
    expectancy_r,
    expectancy_r_ci,
    expectancy_r_p_value,
    expectancy_usd,
    gross_pnl_usd,
    gross_performance_usd,
    information_coefficient,
    max_drawdown,
    monte_carlo_shuffle,
    monthly_usd_per_day,
    net_performance_usd,
    profit_factor,
    sample_size_flag,
    sharpe,
    sortino,
    stats_by_regime,
    sustains_daily_floor,
    sustains_weekly_floor,
    weekly_usd_per_week,
    win_rate,
    win_rate_ci,
)


def _ms(year, month, day=15):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _trade(pnl_usd, entry_time_ms=0, trade_seq=1, r_multiple=None):
    return Trade(trade_seq=trade_seq, side="long", entry_time_ms=entry_time_ms,
                 entry_price=100.0, size_usd=1000.0, signal_snapshot={}, pnl_usd=pnl_usd,
                 r_multiple=r_multiple)


class TestSharpeSortino(unittest.TestCase):
    def test_sharpe_hand_computed(self):
        # media 0.02, desviacion poblacional 0.01 -> sharpe = 2.0 (periods_per_year=1)
        self.assertAlmostEqual(sharpe([0.01, 0.03], periods_per_year=1), 2.0)

    def test_sharpe_zero_variance_is_zero(self):
        self.assertEqual(sharpe([0.02, 0.02, 0.02], periods_per_year=1), 0.0)

    def test_sharpe_needs_at_least_two_points(self):
        self.assertEqual(sharpe([0.01], periods_per_year=1), 0.0)
        self.assertEqual(sharpe([], periods_per_year=1), 0.0)

    def test_sortino_hand_computed(self):
        # media 0.01, unico retorno negativo -0.02 -> downside_dev = 0.02
        self.assertAlmostEqual(sortino([0.04, -0.02], periods_per_year=1), 0.5)

    def test_sortino_no_downside_is_zero(self):
        self.assertEqual(sortino([0.01, 0.03], periods_per_year=1), 0.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_hand_computed_drawdown(self):
        equity = [100.0, 110.0, 90.0, 95.0, 120.0, 80.0]
        # peor caida: 120 -> 80 = 33.33%
        self.assertAlmostEqual(max_drawdown(equity), (120.0 - 80.0) / 120.0)

    def test_monotonic_rise_has_no_drawdown(self):
        self.assertEqual(max_drawdown([100.0, 110.0, 120.0]), 0.0)


class TestWinRate(unittest.TestCase):
    def test_ignores_open_trades_counts_only_closed(self):
        trades = [_trade(10.0), _trade(-5.0), _trade(None), _trade(20.0), _trade(-1.0)]
        self.assertAlmostEqual(win_rate(trades), 0.5)  # 2 de 4 cerrados

    def test_empty_is_zero(self):
        self.assertEqual(win_rate([]), 0.0)


class TestProfitFactorAndGrossNet(unittest.TestCase):
    """PF/gross/net -- goal de laboratorio cuantitativo, puntos 15/16/18:
    faltaban en `summarize()`, que solo tenia win_rate y expectancy en R."""

    def test_profit_factor_hand_computed(self):
        trades = [_trade(100.0), _trade(50.0), _trade(-40.0), _trade(-10.0)]
        # ganancia bruta 150 / perdida bruta 50 = 3.0
        self.assertAlmostEqual(profit_factor(trades), 3.0)

    def test_profit_factor_none_when_no_losses(self):
        trades = [_trade(100.0), _trade(50.0)]
        self.assertIsNone(profit_factor(trades))

    def test_profit_factor_none_when_no_closed_trades(self):
        self.assertIsNone(profit_factor([_trade(None)]))

    def test_gross_pnl_adds_back_costs(self):
        t = Trade(trade_seq=1, side="long", entry_time_ms=0, entry_price=100.0,
                   size_usd=1000.0, signal_snapshot={}, pnl_usd=80.0,
                   commission_usd=15.0, funding_paid_usd=5.0)
        self.assertAlmostEqual(gross_pnl_usd(t), 100.0)

    def test_gross_pnl_none_for_open_trade(self):
        self.assertIsNone(gross_pnl_usd(_trade(None)))

    def test_gross_vs_net_performance_differ_by_costs(self):
        t = Trade(trade_seq=1, side="long", entry_time_ms=0, entry_price=100.0,
                   size_usd=1000.0, signal_snapshot={}, pnl_usd=80.0,
                   commission_usd=15.0, funding_paid_usd=5.0)
        self.assertAlmostEqual(gross_performance_usd([t]), 100.0)
        self.assertAlmostEqual(net_performance_usd([t]), 80.0)

    def test_expectancy_usd_hand_computed(self):
        trades = [_trade(100.0), _trade(-50.0), _trade(30.0)]
        self.assertAlmostEqual(expectancy_usd(trades), 80.0 / 3)

    def test_expectancy_usd_none_when_no_closed_trades(self):
        self.assertIsNone(expectancy_usd([_trade(None)]))


class TestInformationCoefficient(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        self.assertAlmostEqual(information_coefficient([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]), 1.0)

    def test_perfect_negative_correlation(self):
        self.assertAlmostEqual(information_coefficient([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]), -1.0)

    def test_mismatched_lengths_is_zero(self):
        self.assertEqual(information_coefficient([1, 2], [1, 2, 3]), 0.0)


class TestBootstrapCi(unittest.TestCase):
    def test_constant_series_has_degenerate_ci(self):
        lo, hi = bootstrap_ci([1.0] * 10, n_resamples=200, seed=42)
        self.assertAlmostEqual(lo, 1.0)
        self.assertAlmostEqual(hi, 1.0)

    def test_empty_is_zero_zero(self):
        self.assertEqual(bootstrap_ci([]), (0.0, 0.0))

    def test_deterministic_with_seed(self):
        a = bootstrap_ci([1.0, 2.0, -1.0, 3.0], n_resamples=100, seed=7)
        b = bootstrap_ci([1.0, 2.0, -1.0, 3.0], n_resamples=100, seed=7)
        self.assertEqual(a, b)


class TestMonteCarloShuffle(unittest.TestCase):
    def test_identical_pnls_have_zero_drawdown_and_fixed_final_equity(self):
        result = monte_carlo_shuffle([5.0, 5.0, 5.0, 5.0], n_shuffles=100, seed=1)
        self.assertEqual(result["max_drawdown_p50"], 0.0)
        self.assertEqual(result["max_drawdown_p95"], 0.0)
        self.assertAlmostEqual(result["final_equity_p50"], 20.0)

    def test_empty_returns_zeroed_dict(self):
        result = monte_carlo_shuffle([])
        self.assertEqual(result["final_equity_p50"], 0.0)


class TestStatsByRegime(unittest.TestCase):
    def test_buckets_pnl_by_entry_regime(self):
        trades = [_trade(10.0, entry_time_ms=0), _trade(-5.0, entry_time_ms=1), _trade(20.0, entry_time_ms=2)]
        bar_times_ms = [0, 1, 2]
        regime_labels = ["trending_up", "trending_up", "ranging_low_vol"]
        result = stats_by_regime(trades, bar_times_ms, regime_labels)
        self.assertEqual(result["trending_up"]["count"], 2)
        self.assertAlmostEqual(result["trending_up"]["total_pnl_usd"], 5.0)
        self.assertEqual(result["ranging_low_vol"]["count"], 1)


class TestWinRateCi(unittest.TestCase):
    def test_all_winners_gives_a_ci_pinned_at_one(self):
        trades = [_trade(10.0), _trade(5.0), _trade(20.0)]
        lo, hi = win_rate_ci(trades, n_resamples=200, seed=0)
        self.assertAlmostEqual(lo, 1.0)
        self.assertAlmostEqual(hi, 1.0)

    def test_empty_trades_gives_zero_ci(self):
        self.assertEqual(win_rate_ci([]), (0.0, 0.0))

    def test_is_deterministic_with_a_seed(self):
        trades = [_trade(10.0), _trade(-5.0), _trade(20.0), _trade(-1.0)]
        a = win_rate_ci(trades, n_resamples=100, seed=7)
        b = win_rate_ci(trades, n_resamples=100, seed=7)
        self.assertEqual(a, b)


class TestExpectancyR(unittest.TestCase):
    def test_averages_only_trades_with_a_defined_r_multiple(self):
        """Mezclar trades con y sin SL inventaria una unidad de riesgo
        comun que no existe para los que no tienen SL -- deben ignorarse,
        no contar como 0."""
        trades = [_trade(10.0, r_multiple=2.0), _trade(-5.0, r_multiple=-1.0), _trade(999.0, r_multiple=None)]
        self.assertAlmostEqual(expectancy_r(trades), 0.5)

    def test_no_trades_with_r_multiple_returns_none_not_zero(self):
        """None -- no 0.0 -- distingue "sin dato" de "expectancy nula"."""
        trades = [_trade(10.0, r_multiple=None), _trade(-5.0, r_multiple=None)]
        self.assertIsNone(expectancy_r(trades))
        self.assertIsNone(expectancy_r([]))

    def test_ci_lower_bound_is_below_the_point_estimate_for_noisy_data(self):
        trades = [_trade(0.0, r_multiple=r) for r in [3.0, -1.0, 2.0, -2.0, 1.0]]
        point = expectancy_r(trades)
        lo, hi = expectancy_r_ci(trades, n_resamples=500, seed=0)
        self.assertLess(lo, point)
        self.assertGreater(hi, point)

    def test_ci_is_none_when_no_trade_has_a_stop(self):
        trades = [_trade(10.0, r_multiple=None)]
        self.assertIsNone(expectancy_r_ci(trades))


class TestExpectancyRPValue(unittest.TestCase):
    def test_consistently_positive_r_gives_a_low_p_value(self):
        trades = [_trade(0.0, r_multiple=r) for r in [2.0, 1.5, 2.5, 1.8, 2.2, 1.9, 2.1, 1.7]]
        p = expectancy_r_p_value(trades, n_resamples=500, seed=0)
        self.assertLess(p, 0.05)

    def test_mixed_around_zero_gives_a_high_p_value(self):
        trades = [_trade(0.0, r_multiple=r) for r in [1.0, -1.0, 1.0, -1.0, 0.5, -0.5]]
        p = expectancy_r_p_value(trades, n_resamples=500, seed=0)
        self.assertGreater(p, 0.2)

    def test_no_r_multiples_returns_none(self):
        self.assertIsNone(expectancy_r_p_value([_trade(10.0, r_multiple=None)]))


class TestBenjaminiHochberg(unittest.TestCase):
    def test_all_low_p_values_survive(self):
        result = benjamini_hochberg([0.001, 0.002, 0.003], alpha=0.05)
        self.assertEqual(result, [True, True, True])

    def test_all_high_p_values_are_rejected(self):
        result = benjamini_hochberg([0.8, 0.9, 0.95], alpha=0.05)
        self.assertEqual(result, [False, False, False])

    def test_none_p_values_never_survive(self):
        """Una configuracion sin SL (sin p-valor que evaluar) no puede
        "sobrevivir" una correccion que nunca se le aplico de verdad."""
        result = benjamini_hochberg([0.001, None, 0.9], alpha=0.05)
        self.assertEqual(result, [True, False, False])

    def test_many_mediocre_candidates_do_not_all_pass_by_chance(self):
        """El caso concreto que motivo el criterio:
        con muchas candidatas mediocres evaluadas (un p-valor uniforme en
        [0,1], el peor caso posible para BH), la correccion debe rechazar
        la gran mayoria -- no dejar pasar ~1 de cada 11 como haria un
        umbral simple sin corregir."""
        import random as _random
        rng = _random.Random(42)
        p_values = [rng.random() for _ in range(300)]
        result = benjamini_hochberg(p_values, alpha=0.05)
        self.assertLess(sum(result), 30)  # muy por debajo de un 10% suelto


class TestSampleSizeFlag(unittest.TestCase):
    def test_below_two_hundred_closed_trades_is_insufficient(self):
        trades = [_trade(1.0) for _ in range(199)]
        self.assertEqual(sample_size_flag(trades), "INSUFFICIENT_SAMPLE")

    def test_two_hundred_or_more_closed_trades_is_ok(self):
        trades = [_trade(1.0) for _ in range(200)]
        self.assertEqual(sample_size_flag(trades), "OK")

    def test_open_trades_without_pnl_do_not_count(self):
        trades = [_trade(1.0) for _ in range(199)] + [Trade(
            trade_seq=200, side="long", entry_time_ms=0, entry_price=100.0,
            size_usd=1000.0, signal_snapshot={}, pnl_usd=None,
        )]
        self.assertEqual(sample_size_flag(trades), "INSUFFICIENT_SAMPLE")


class TestMonthlyUsdPerDay(unittest.TestCase):
    def test_splits_total_pnl_by_calendar_days_in_month(self):
        # enero 2026: 31 dias, $310 repartidos entre dos trades -> $10/dia
        trades = [_trade(200.0, entry_time_ms=_ms(2026, 1, 5)),
                  _trade(110.0, entry_time_ms=_ms(2026, 1, 25))]
        result = monthly_usd_per_day(trades)
        self.assertAlmostEqual(result["2026-01"], 310.0 / 31)

    def test_february_uses_its_own_day_count(self):
        # 2026 no es bisiesto -> febrero tiene 28 dias
        trades = [_trade(280.0, entry_time_ms=_ms(2026, 2, 10))]
        result = monthly_usd_per_day(trades)
        self.assertAlmostEqual(result["2026-02"], 10.0)

    def test_open_trades_excluded(self):
        trades = [Trade(trade_seq=1, side="long", entry_time_ms=_ms(2026, 1, 5),
                         entry_price=100.0, size_usd=1000.0, signal_snapshot={}, pnl_usd=None)]
        self.assertEqual(monthly_usd_per_day(trades), {})

    def test_month_with_no_trades_is_absent_not_zero(self):
        trades = [_trade(100.0, entry_time_ms=_ms(2026, 1, 5))]
        result = monthly_usd_per_day(trades)
        self.assertNotIn("2026-02", result)


class TestConsecutiveMonthsMeetingFloor(unittest.TestCase):
    def test_streak_broken_by_month_below_floor(self):
        monthly = {"2026-01": 10.0, "2026-02": 10.0, "2026-03": 5.0, "2026-04": 12.0, "2026-05": 15.0}
        self.assertEqual(consecutive_months_meeting_floor(monthly, floor_usd_per_day=8.0), 2)

    def test_streak_broken_by_missing_calendar_month(self):
        # falta 2026-02 en el dict -- no es "adyacente en la lista", es un
        # hueco real que no debe unir enero con marzo en una sola racha
        monthly = {"2026-01": 10.0, "2026-03": 10.0}
        self.assertEqual(consecutive_months_meeting_floor(monthly, floor_usd_per_day=8.0), 1)

    def test_full_streak_when_every_month_qualifies(self):
        monthly = {"2026-01": 10.0, "2026-02": 11.0, "2026-03": 9.0}
        self.assertEqual(consecutive_months_meeting_floor(monthly, floor_usd_per_day=8.0), 3)

    def test_empty_monthly_is_zero(self):
        self.assertEqual(consecutive_months_meeting_floor({}, floor_usd_per_day=8.0), 0)

    def test_year_boundary_is_contiguous(self):
        monthly = {"2025-12": 10.0, "2026-01": 10.0, "2026-02": 10.0}
        self.assertEqual(consecutive_months_meeting_floor(monthly, floor_usd_per_day=8.0), 3)


class TestSustainsDailyFloor(unittest.TestCase):
    def test_true_when_three_consecutive_months_meet_floor(self):
        trades = [
            _trade(310.0, entry_time_ms=_ms(2026, 1, 5)),   # $10/dia (31 dias)
            _trade(280.0, entry_time_ms=_ms(2026, 2, 5)),   # $10/dia (28 dias)
            _trade(310.0, entry_time_ms=_ms(2026, 3, 5)),   # $10/dia (31 dias)
        ]
        self.assertTrue(sustains_daily_floor(trades, floor_usd_per_day=8.0, min_consecutive_months=3))

    def test_false_when_only_two_consecutive_months(self):
        trades = [
            _trade(310.0, entry_time_ms=_ms(2026, 1, 5)),
            _trade(280.0, entry_time_ms=_ms(2026, 2, 5)),
        ]
        self.assertFalse(sustains_daily_floor(trades, floor_usd_per_day=8.0, min_consecutive_months=3))


class TestWeeklyUsdPerWeek(unittest.TestCase):
    """Pedido del usuario (2026-09-19): la meta real es semanal
    ("+0.5% a +3% o mas a la semana"), no diaria -- pensada para
    una estrategia de pocos trades de alta conviccion (2-3/semana)."""

    def test_sums_within_the_same_iso_week_no_division(self):
        # Lunes 2026-01-05 y domingo 2026-01-11 -- misma semana ISO
        # (2026-W02) -- a diferencia de `monthly_usd_per_day`, la suma
        # semanal NO se divide entre dias.
        trades = [_trade(30.0, entry_time_ms=_ms(2026, 1, 5)),
                  _trade(20.0, entry_time_ms=_ms(2026, 1, 11))]
        self.assertEqual(weekly_usd_per_week(trades), {"2026-W02": 50.0})

    def test_open_trades_excluded(self):
        trades = [Trade(trade_seq=1, side="long", entry_time_ms=_ms(2026, 1, 5),
                         entry_price=100.0, size_usd=1000.0, signal_snapshot={}, pnl_usd=None)]
        self.assertEqual(weekly_usd_per_week(trades), {})

    def test_week_with_no_trades_is_absent_not_zero(self):
        trades = [_trade(100.0, entry_time_ms=_ms(2026, 1, 5))]
        result = weekly_usd_per_week(trades)
        self.assertNotIn("2026-W03", result)


class TestConsecutiveWeeksMeetingFloor(unittest.TestCase):
    def test_streak_broken_by_week_below_floor(self):
        weekly = {"2026-W01": 10.0, "2026-W02": 10.0, "2026-W03": 5.0, "2026-W04": 12.0}
        self.assertEqual(consecutive_weeks_meeting_floor(weekly, floor_usd_per_week=8.0), 2)

    def test_streak_broken_by_missing_calendar_week(self):
        weekly = {"2026-W01": 10.0, "2026-W03": 10.0}
        self.assertEqual(consecutive_weeks_meeting_floor(weekly, floor_usd_per_week=8.0), 1)

    def test_full_streak_when_every_week_qualifies(self):
        weekly = {"2026-W01": 10.0, "2026-W02": 11.0, "2026-W03": 9.0}
        self.assertEqual(consecutive_weeks_meeting_floor(weekly, floor_usd_per_week=8.0), 3)

    def test_empty_weekly_is_zero(self):
        self.assertEqual(consecutive_weeks_meeting_floor({}, floor_usd_per_week=8.0), 0)

    def test_year_boundary_is_contiguous(self):
        # 2025-W52 es la ULTIMA semana ISO de 2025 (2025 no tiene W53,
        # verificado con datetime.isocalendar) -- seguida de 2026-W01,
        # cronologicamente consecutiva pese a cruzar el año.
        weekly = {"2025-W52": 10.0, "2026-W01": 10.0}
        self.assertEqual(consecutive_weeks_meeting_floor(weekly, floor_usd_per_week=8.0), 2)


class TestSustainsWeeklyFloor(unittest.TestCase):
    def test_true_when_three_consecutive_weeks_meet_floor(self):
        trades = [
            _trade(10.0, entry_time_ms=_ms(2026, 1, 5)),   # 2026-W02
            _trade(10.0, entry_time_ms=_ms(2026, 1, 12)),  # 2026-W03
            _trade(10.0, entry_time_ms=_ms(2026, 1, 19)),  # 2026-W04
        ]
        self.assertTrue(sustains_weekly_floor(trades, floor_usd_per_week=8.0, min_consecutive_weeks=3))

    def test_false_when_only_two_consecutive_weeks(self):
        trades = [
            _trade(10.0, entry_time_ms=_ms(2026, 1, 5)),
            _trade(10.0, entry_time_ms=_ms(2026, 1, 12)),
        ]
        self.assertFalse(sustains_weekly_floor(trades, floor_usd_per_week=8.0, min_consecutive_weeks=3))


if __name__ == "__main__":
    unittest.main()
