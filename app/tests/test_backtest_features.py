import math
import unittest

from backtest.data import Bar, OiPoint
from backtest.features import (
    candle_body_ratio,
    log_returns,
    momentum,
    price_oi_divergence,
    prior_rolling_max_high,
    prior_rolling_min_low,
    realized_vol,
    rolling_zscore,
    taker_buy_ratio,
)


def _bar(close, open_=None, high=None, low=None, volume=100.0, taker_buy=50.0, time_ms=0):
    o = open_ if open_ is not None else close
    h = high if high is not None else max(o, close)
    l = low if low is not None else min(o, close)
    return Bar(time_ms, o, h, l, close, volume, taker_buy)


class TestLogReturns(unittest.TestCase):
    def test_first_bar_is_zero(self):
        self.assertEqual(log_returns([_bar(100.0)])[0], 0.0)

    def test_hand_computed(self):
        bars = [_bar(100.0), _bar(110.0)]
        rets = log_returns(bars)
        self.assertAlmostEqual(rets[1], math.log(110.0 / 100.0), places=9)


class TestRealizedVol(unittest.TestCase):
    def test_constant_price_has_zero_vol(self):
        bars = [_bar(100.0) for _ in range(10)]
        vols = realized_vol(bars, window=5)
        self.assertTrue(all(v == 0.0 for v in vols))

    def test_single_bar_warmup_is_zero(self):
        self.assertEqual(realized_vol([_bar(100.0)])[0], 0.0)


class TestCandleBodyRatio(unittest.TestCase):
    def test_full_body_no_wick(self):
        b = _bar(close=110.0, open_=100.0, high=110.0, low=100.0)
        self.assertAlmostEqual(candle_body_ratio([b])[0], 1.0)

    def test_doji_small_body_large_range(self):
        b = _bar(close=100.5, open_=100.0, high=110.0, low=90.0)
        self.assertAlmostEqual(candle_body_ratio([b])[0], 0.5 / 20.0)

    def test_zero_range_is_zero_not_division_error(self):
        b = _bar(close=100.0, open_=100.0, high=100.0, low=100.0)
        self.assertEqual(candle_body_ratio([b])[0], 0.0)


class TestTakerBuyRatio(unittest.TestCase):
    def test_hand_computed(self):
        b = _bar(close=100.0, volume=100.0, taker_buy=70.0)
        self.assertAlmostEqual(taker_buy_ratio([b])[0], 0.7)

    def test_zero_volume_is_neutral(self):
        b = _bar(close=100.0, volume=0.0, taker_buy=0.0)
        self.assertEqual(taker_buy_ratio([b])[0], 0.5)


class TestRollingZscore(unittest.TestCase):
    def test_flat_series_is_zero(self):
        self.assertEqual(rolling_zscore([5.0, 5.0, 5.0, 5.0], window=3)[-1], 0.0)

    def test_warmup_single_point_is_zero(self):
        self.assertEqual(rolling_zscore([5.0], window=3)[0], 0.0)

    def test_outlier_gets_large_zscore(self):
        z = rolling_zscore([1.0, 1.0, 1.0, 1.0, 100.0], window=5)
        self.assertGreater(z[-1], 1.5)


class TestMomentum(unittest.TestCase):
    def test_warmup_is_zero(self):
        bars = [_bar(100.0), _bar(105.0)]
        self.assertEqual(momentum(bars, lookback=5)[1], 0.0)

    def test_hand_computed(self):
        bars = [_bar(100.0)] * 5 + [_bar(110.0)]
        self.assertAlmostEqual(momentum(bars, lookback=5)[5], 0.10)


class TestPriorRollingMaxHigh(unittest.TestCase):
    def test_excludes_the_current_bar(self):
        bars = [_bar(close=100.0, high=100.0), _bar(close=200.0, high=200.0)]
        out = prior_rolling_max_high(bars, window=5)
        # la barra 1 (high=200) no debe contarse contra si misma
        self.assertEqual(out[1], 100.0)

    def test_warmup_is_infinity_not_a_trigger(self):
        out = prior_rolling_max_high([_bar(close=100.0, high=100.0)], window=5)
        self.assertEqual(out[0], float("inf"))

    def test_only_looks_at_the_trailing_window(self):
        bars = [_bar(close=c, high=c) for c in [500.0, 100.0, 101.0, 102.0]]
        out = prior_rolling_max_high(bars, window=2)
        # en i=3, ventana = barras 1,2 (high 100,101) -- la barra 0 (500) ya no cuenta
        self.assertEqual(out[3], 101.0)


class TestPriorRollingMinLow(unittest.TestCase):
    def test_excludes_the_current_bar(self):
        bars = [_bar(close=100.0, low=100.0), _bar(close=10.0, low=10.0)]
        out = prior_rolling_min_low(bars, window=5)
        self.assertEqual(out[1], 100.0)

    def test_warmup_is_negative_infinity_not_a_trigger(self):
        out = prior_rolling_min_low([_bar(close=100.0, low=100.0)], window=5)
        self.assertEqual(out[0], float("-inf"))


class TestPriceOiDivergence(unittest.TestCase):
    def test_price_up_oi_flat_is_positive(self):
        bars = [_bar(100.0)] * 3 + [_bar(110.0)]
        oi_rows = [OiPoint(0, 1000.0, None, None) for _ in range(4)]
        div = price_oi_divergence(bars, oi_rows, lookback=3)
        self.assertGreater(div[3], 0.0)

    def test_warmup_is_zero(self):
        bars = [_bar(100.0), _bar(105.0)]
        oi_rows = [OiPoint(0, 1000.0, None, None), OiPoint(0, 1000.0, None, None)]
        self.assertEqual(price_oi_divergence(bars, oi_rows, lookback=5)[1], 0.0)


if __name__ == "__main__":
    unittest.main()
