import unittest

from backtest.data import Bar
from backtest.ta import RSI_NEUTRAL, rsi


def _bar(t, close):
    return Bar(t, close, close, close, close, 0.0, 0.0)


class TestRsi(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(rsi([]), [])

    def test_neutral_during_warmup(self):
        bars = [_bar(i, 100.0 + i) for i in range(10)]  # period=14 por defecto, nunca calienta
        self.assertEqual(rsi(bars, period=14), [RSI_NEUTRAL] * 10)

    def test_all_gains_saturates_at_100(self):
        # Sube siempre, ni una barra roja -- avg_loss se queda en 0.
        bars = [_bar(i, 100.0 + i) for i in range(20)]
        out = rsi(bars, period=14)
        self.assertAlmostEqual(out[-1], 100.0)

    def test_all_losses_bottoms_near_zero(self):
        bars = [_bar(i, 100.0 - i) for i in range(20)]
        out = rsi(bars, period=14)
        self.assertAlmostEqual(out[-1], 0.0)

    def test_flat_price_is_neutral_no_division_by_zero(self):
        # Sin gain ni loss -- avg_gain=avg_loss=0, debe caer al caso
        # neutral (RSI_NEUTRAL), no una excepcion de division por cero.
        bars = [_bar(i, 100.0) for i in range(20)]
        out = rsi(bars, period=14)
        self.assertEqual(out[-1], RSI_NEUTRAL)

    def test_reference_value_wilder_example(self):
        # Serie clasica de referencia (Wilder, "New Concepts in Technical
        # Trading Systems", el ejemplo de 14 periodos que casi cualquier
        # implementacion de RSI usa para verificarse) -- RSI(14) en la
        # ultima barra debe caer cerca de 70 (no exacto: distintas fuentes
        # redondean los precios de forma ligeramente distinta).
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        bars = [_bar(i, c) for i, c in enumerate(closes)]
        out = rsi(bars, period=14)
        self.assertAlmostEqual(out[-1], 70.5, delta=1.5)

    def test_causal_future_bars_do_not_affect_past_values(self):
        bars = [_bar(i, 100.0 + (i % 5)) for i in range(30)]
        full = rsi(bars, period=14)
        truncated = rsi(bars[:20], period=14)
        self.assertEqual(full[:20], truncated)


if __name__ == "__main__":
    unittest.main()
