import unittest

from backtest.data import Bar
from backtest.regime import (
    REGIME_RANGING_HIGH_VOL,
    REGIME_RANGING_LOW_VOL,
    REGIME_TRENDING_DOWN,
    REGIME_TRENDING_UP,
    atr,
    classify_regime,
    sma,
    true_range,
)


def _flat_bars(n, amplitude, base=100.0, start_t=0):
    """`n` barras sin deriva (close siempre = base) con rango [base-amplitude,
    base+amplitude] -- aisla el eje de volatilidad del de tendencia: la SMA
    de los closes es constante, asi que `classify_regime` nunca puede
    confundir esto con una tendencia."""
    return [Bar(start_t + i, base, base + amplitude, base - amplitude, base, 10.0, 5.0) for i in range(n)]


def _ramp_bars(n, start=100.0, step=1.0, start_t=0):
    return [Bar(start_t + i, start + i * step, start + i * step + 0.1,
                start + i * step - 0.1, start + i * step, 10.0, 5.0) for i in range(n)]


class TestTrueRangeAndAtr(unittest.TestCase):
    def test_true_range_first_bar_is_high_minus_low(self):
        bars = [Bar(0, 100.0, 105.0, 95.0, 102.0, 1.0, 0.5)]
        self.assertEqual(true_range(bars), [10.0])

    def test_true_range_considers_gap_from_prev_close(self):
        bars = [Bar(0, 100.0, 101.0, 99.0, 100.0, 1.0, 0.5),
                Bar(1, 100.0, 110.0, 108.0, 109.0, 1.0, 0.5)]  # gap up
        trs = true_range(bars)
        self.assertEqual(trs[1], 10.0)  # |110 - 100| = 10, mayor que high-low=2

    def test_atr_constant_range_converges_to_that_range(self):
        bars = _flat_bars(30, amplitude=5.0)
        atrs = atr(bars, period=14)
        self.assertAlmostEqual(atrs[-1], 10.0)  # TR = 2*amplitude en cada barra


class TestSma(unittest.TestCase):
    def test_matches_hand_computed_average(self):
        self.assertEqual(sma([1, 2, 3, 4], period=2), [1.0, 1.5, 2.5, 3.5])


class TestClassifyRegime(unittest.TestCase):
    def test_rising_ramp_is_trending_up(self):
        bars = _ramp_bars(60, start=100.0, step=1.0)
        labels = classify_regime(bars)
        self.assertEqual(labels[-1], REGIME_TRENDING_UP)

    def test_falling_ramp_is_trending_down(self):
        bars = _ramp_bars(60, start=200.0, step=-1.0)
        labels = classify_regime(bars)
        self.assertEqual(labels[-1], REGIME_TRENDING_DOWN)

    def test_high_then_low_volatility_no_trend(self):
        # sin deriva en ningun punto (closes constantes en 100) -- separa
        # limpiamente el eje de volatilidad del de tendencia.
        bars = _flat_bars(30, amplitude=10.0, start_t=0) + _flat_bars(30, amplitude=0.2, start_t=30)
        labels = classify_regime(bars)
        self.assertEqual(labels[25], REGIME_RANGING_HIGH_VOL)
        self.assertEqual(labels[55], REGIME_RANGING_LOW_VOL)

    def test_empty_is_empty(self):
        self.assertEqual(classify_regime([]), [])


if __name__ == "__main__":
    unittest.main()
