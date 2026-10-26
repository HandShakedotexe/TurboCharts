import unittest

from backtest.strategies.volume_breakout import VolumeBreakoutLong, VolumeBreakoutShort, _sized_position


class _FakeBar:
    def __init__(self, close):
        self.close = close


class _FakePosition:
    def __init__(self, side):
        self.side = side


class _FakeCtx:
    def __init__(self, side, close, prior_max_high=100.0, prior_min_low=90.0,
                 volume_zscore=0.0, atr=1.0, regime="trending_up"):
        self.position = _FakePosition(side)
        self.bar = _FakeBar(close)
        self.indicators = {
            "prior_max_high_20": prior_max_high, "prior_min_low_20": prior_min_low,
            "volume_zscore_60": volume_zscore, "atr": atr, "regime": regime,
        }


class TestVolumeBreakoutLong(unittest.TestCase):
    def test_no_breakout_no_entry(self):
        strat = VolumeBreakoutLong()
        ctx = _FakeCtx(side="flat", close=99.0, prior_max_high=100.0, volume_zscore=5.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_breakout_without_volume_no_entry(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, volume_zscore=0.5)
        self.assertIsNone(strat.on_bar(ctx))

    def test_breakout_with_volume_enters_long(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0, stop_atr_mult=2.0)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, volume_zscore=2.5, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 99.0)
        self.assertGreater(target.take_profit_price, 101.0)

    def test_no_entry_while_in_a_position(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0)
        ctx = _FakeCtx(side="long", close=101.0, prior_max_high=100.0, volume_zscore=5.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_warmup_infinity_never_triggers(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0)
        ctx = _FakeCtx(side="flat", close=1_000_000.0, prior_max_high=float("inf"), volume_zscore=10.0)
        self.assertIsNone(strat.on_bar(ctx))


class TestVolumeBreakoutTrendFilter(unittest.TestCase):
    def test_long_requires_trending_up_when_enabled(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0, require_trend_regime=True)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, volume_zscore=5.0, regime="ranging_low_vol")
        self.assertIsNone(strat.on_bar(ctx))
        ctx2 = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, volume_zscore=5.0, regime="trending_up")
        self.assertIsNotNone(strat.on_bar(ctx2))

    def test_long_ignores_regime_when_disabled(self):
        strat = VolumeBreakoutLong(volume_zscore_threshold=2.0, require_trend_regime=False)
        ctx = _FakeCtx(side="flat", close=101.0, prior_max_high=100.0, volume_zscore=5.0, regime="ranging_low_vol")
        self.assertIsNotNone(strat.on_bar(ctx))

    def test_short_requires_trending_down_when_enabled(self):
        strat = VolumeBreakoutShort(volume_zscore_threshold=2.0, require_trend_regime=True)
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, volume_zscore=5.0, regime="trending_up")
        self.assertIsNone(strat.on_bar(ctx))
        ctx2 = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, volume_zscore=5.0, regime="trending_down")
        self.assertIsNotNone(strat.on_bar(ctx2))


class TestSizedPosition(unittest.TestCase):
    def test_fixed_notional_when_no_risk_target(self):
        self.assertEqual(_sized_position(price=100.0, stop_dist=2.0, size_usd=500.0,
                                          risk_usd_target=None, max_size_usd=10_000.0), 500.0)

    def test_risk_adjusted_scales_inversely_with_stop_distance(self):
        # arriesgar $10 con un stop de $1 (a precio 100) -> notional $1000
        tight = _sized_position(price=100.0, stop_dist=1.0, size_usd=500.0,
                                 risk_usd_target=10.0, max_size_usd=10_000.0)
        wide = _sized_position(price=100.0, stop_dist=4.0, size_usd=500.0,
                                risk_usd_target=10.0, max_size_usd=10_000.0)
        self.assertAlmostEqual(tight, 1000.0)
        self.assertAlmostEqual(wide, 250.0)
        self.assertGreater(tight, wide)

    def test_capped_at_max_size_usd(self):
        # stop casi cero (ATR casi plano) explotaria el notional sin el techo
        size = _sized_position(price=100.0, stop_dist=0.001, size_usd=500.0,
                                risk_usd_target=10.0, max_size_usd=10_000.0)
        self.assertEqual(size, 10_000.0)

    def test_zero_stop_distance_is_zero_not_a_crash(self):
        size = _sized_position(price=100.0, stop_dist=0.0, size_usd=500.0,
                                risk_usd_target=10.0, max_size_usd=10_000.0)
        self.assertEqual(size, 0.0)


class TestVolumeBreakoutShort(unittest.TestCase):
    def test_breakdown_with_volume_enters_short(self):
        strat = VolumeBreakoutShort(volume_zscore_threshold=2.0, stop_atr_mult=2.0)
        ctx = _FakeCtx(side="flat", close=89.0, prior_min_low=90.0, volume_zscore=2.5, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 91.0)
        self.assertLess(target.take_profit_price, 89.0)


if __name__ == "__main__":
    unittest.main()
