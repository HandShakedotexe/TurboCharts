import unittest

from backtest.strategies.cvd_divergence import CvdDivergenceLong, CvdDivergenceShort


class _FakeBar:
    def __init__(self, close):
        self.close = close


class _FakePosition:
    def __init__(self, side):
        self.side = side


class _FakeCtx:
    def __init__(self, side, close, prior_max_high=100.0, prior_min_low=90.0,
                 cvd_change_20=0.0, atr=1.0):
        self.position = _FakePosition(side)
        self.bar = _FakeBar(close)
        self.indicators = {
            "prior_max_high_20": prior_max_high, "prior_min_low_20": prior_min_low,
            "cvd_change_20": cvd_change_20, "atr": atr,
        }


class TestCvdDivergenceLong(unittest.TestCase):
    def test_no_new_low_no_entry(self):
        strat = CvdDivergenceLong()
        ctx = _FakeCtx(side="flat", close=91.0, prior_min_low=90.0, cvd_change_20=10.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_new_low_without_positive_cvd_no_entry(self):
        strat = CvdDivergenceLong()
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=-5.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_new_low_with_positive_cvd_enters_long(self):
        strat = CvdDivergenceLong(stop_atr_mult=2.0)
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 87.0)
        self.assertGreater(target.take_profit_price, 89.0)

    def test_no_entry_while_in_a_position(self):
        strat = CvdDivergenceLong()
        ctx = _FakeCtx(side="long", close=89.0, prior_min_low=90.0, cvd_change_20=10.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_trail_and_target_atr_mult_pass_through_to_the_target_position(self):
        strat = CvdDivergenceLong(trail_atr_mult=1.5, target_atr_mult=3.0)
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.trail_atr_mult, 1.5)
        self.assertEqual(target.target_atr_mult, 3.0)

    def test_breakeven_at_r_passes_through_to_the_target_position(self):
        """`cvd_divergence_breakeven_long` (2026-09-19) -- misma clase,
        solo agrega este parametro."""
        strat = CvdDivergenceLong(breakeven_at_r=0.2)
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.breakeven_at_r, 0.2)

    def test_defaults_to_no_adaptive_sl_tp(self):
        strat = CvdDivergenceLong()
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertIsNone(target.trail_atr_mult)
        self.assertIsNone(target.target_atr_mult)
        self.assertIsNone(target.breakeven_at_r)

    def test_zero_cvd_change_is_not_a_divergence(self):
        # >0 estricto, no >=0 -- sin flujo neto no hay divergencia que
        # apostar, solo ruido.
        strat = CvdDivergenceLong()
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, cvd_change_20=0.0)
        self.assertIsNone(strat.on_bar(ctx))


class TestCvdDivergenceShort(unittest.TestCase):
    def test_new_high_with_negative_cvd_enters_short(self):
        strat = CvdDivergenceShort(stop_atr_mult=2.0)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, cvd_change_20=-5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 103.0)
        self.assertLess(target.take_profit_price, 101.0)

    def test_new_high_without_negative_cvd_no_entry(self):
        strat = CvdDivergenceShort()
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, cvd_change_20=5.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_breakeven_at_r_passes_through_to_the_target_position(self):
        strat = CvdDivergenceShort(breakeven_at_r=0.3)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, cvd_change_20=-5.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.breakeven_at_r, 0.3)


if __name__ == "__main__":
    unittest.main()
