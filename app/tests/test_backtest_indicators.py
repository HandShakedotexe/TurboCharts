import unittest

from api.heatmap_model import LiquidationLevel
from backtest.data import Bar, FundingPoint, OiPoint
from backtest.indicators import (
    bar_delta,
    cvd,
    cvd_change,
    funding_zscore,
    liquidation_cluster_proximity,
    long_short_ratio_trend,
    oi_trend,
    oi_trend_zscore,
)


def _bar(t, close=100.0, volume=10.0, taker_buy=5.0):
    return Bar(t, close, close, close, close, volume, taker_buy)


class TestBarDelta(unittest.TestCase):
    def test_delta_per_bar_not_accumulated(self):
        """A diferencia de `cvd()`, cada valor es el Delta de SU PROPIA
        vela -- no una suma corrida."""
        bars = [
            _bar(0, volume=10.0, taker_buy=7.0),   # delta = 7 - 3 = 4
            _bar(1, volume=10.0, taker_buy=2.0),   # delta = 2 - 8 = -6
            _bar(2, volume=10.0, taker_buy=10.0),  # delta = 10 - 0 = 10
        ]
        self.assertEqual(bar_delta(bars), [4.0, -6.0, 10.0])

    def test_empty_is_empty(self):
        self.assertEqual(bar_delta([]), [])


class TestCvd(unittest.TestCase):
    def test_accumulates_taker_buy_minus_sell(self):
        bars = [
            _bar(0, volume=10.0, taker_buy=7.0),   # delta = 7 - 3 = 4
            _bar(1, volume=10.0, taker_buy=2.0),   # delta = 2 - 8 = -6, running -2
            _bar(2, volume=10.0, taker_buy=10.0),  # delta = 10 - 0 = 10, running 8
        ]
        self.assertEqual(cvd(bars), [4.0, -2.0, 8.0])

    def test_empty_is_empty(self):
        self.assertEqual(cvd([]), [])


class TestCvdChange(unittest.TestCase):
    def test_warmup_uses_change_since_series_start(self):
        # cvd() acumulado: [4, -2, 8] -- con lookback=5 (mas grande que la
        # serie entera), cada punto compara contra el INICIO (indice 0),
        # nunca contra un indice negativo.
        bars = [
            _bar(0, volume=10.0, taker_buy=7.0),
            _bar(1, volume=10.0, taker_buy=2.0),
            _bar(2, volume=10.0, taker_buy=10.0),
        ]
        self.assertEqual(cvd_change(bars, lookback=5), [0.0, -6.0, 4.0])

    def test_after_warmup_compares_against_a_fixed_window(self):
        bars = [_bar(i, volume=10.0, taker_buy=7.0) for i in range(5)]  # cvd: 4,8,12,16,20
        # lookback=2: indice 3 compara contra indice 1 (16-8=8), indice 4
        # contra indice 2 (20-12=8).
        result = cvd_change(bars, lookback=2)
        self.assertEqual(result[3], 8.0)
        self.assertEqual(result[4], 8.0)

    def test_empty_is_empty(self):
        self.assertEqual(cvd_change([], lookback=5), [])


class TestOiTrend(unittest.TestCase):
    def test_zero_during_warmup(self):
        rows = [OiPoint(i, 100.0 + i, None, None) for i in range(5)]
        out = oi_trend(rows, lookback=12)
        self.assertEqual(out, [0.0] * 5)

    def test_rate_of_change_after_warmup(self):
        # rampa: sum_open_interest = 100, 110, 120, ... (paso de 10)
        rows = [OiPoint(i, 100.0 + i * 10, None, None) for i in range(15)]
        out = oi_trend(rows, lookback=12)
        # indice 12: comparado con indice 0 (100 -> 220): (220-100)/100 = 1.2
        self.assertAlmostEqual(out[12], 1.2)

    def test_guards_against_zero_previous(self):
        rows = [OiPoint(0, 0.0, None, None)] + [OiPoint(i, 50.0, None, None) for i in range(1, 13)]
        out = oi_trend(rows, lookback=12)
        self.assertEqual(out[12], 0.0)  # prev=0 -> no se divide por cero


class TestOiTrendZscore(unittest.TestCase):
    def test_flat_trend_is_zero(self):
        """OI creciendo a una TASA PORCENTUAL constante (geometrica, no
        lineal -- una rampa lineal da una tasa de cambio DECRECIENTE, no
        plana) da un `oi_trend` plano; sin variacion en la ventana, el
        z-score debe ser 0, no NaN/division por cero."""
        rows = [OiPoint(i, 100.0 * (1.05 ** i), None, None) for i in range(40)]
        out = oi_trend_zscore(rows, trend_lookback=12, zscore_window=10)
        # antes de este indice, la ventana de z-score todavia mezcla ceros
        # del calentamiento de oi_trend (indices < 12) con el valor
        # constante real -- no es plana todavia, y no deberia serlo.
        warmup_clear = 12 + 10 - 1
        self.assertTrue(all(abs(z) < 1e-6 for z in out[warmup_clear:]))

    def test_a_sudden_spike_gets_a_large_positive_zscore(self):
        """El mismo caso real que motivo este indicador (2026-09-03):
        BTCUSDT 2020-2026 tiene oi_trend con media ~0 y desviacion ~0.028
        -- un umbral FIJO (p.ej. 0.05) solo se cumple 0.08% de las veces,
        demasiado raro. Un pico puntual sobre una base tranquila si debe
        marcar un z-score alto, sin importar la escala absoluta."""
        rows = [OiPoint(i, 100.0, None, None) for i in range(20)]  # OI plano -> oi_trend ~0
        rows.append(OiPoint(20, 130.0, None, None))  # salto puntual -> oi_trend alto en ese punto
        out = oi_trend_zscore(rows, trend_lookback=1, zscore_window=15)
        self.assertGreater(out[-1], 2.0)

    def test_warmup_with_fewer_than_two_points_is_zero(self):
        rows = [OiPoint(0, 100.0, None, None)]
        self.assertEqual(oi_trend_zscore(rows), [0.0])


class TestFundingZscore(unittest.TestCase):
    def test_flat_series_is_zero(self):
        rows = [FundingPoint(i, 0.0001) for i in range(10)]
        out = funding_zscore(rows, lookback=8)
        self.assertTrue(all(z == 0.0 for z in out))

    def test_outlier_gets_large_positive_zscore(self):
        rows = [FundingPoint(i, 0.0001) for i in range(7)] + [FundingPoint(7, 0.01)]
        out = funding_zscore(rows, lookback=8)
        self.assertGreater(out[-1], 2.0)

    def test_symmetric_negative_outlier(self):
        rows = [FundingPoint(i, 0.0001) for i in range(7)] + [FundingPoint(7, -0.01)]
        out = funding_zscore(rows, lookback=8)
        self.assertLess(out[-1], -2.0)


class TestLongShortRatioTrend(unittest.TestCase):
    def test_rate_of_change(self):
        rows = [OiPoint(i, None, 1.0 + i * 0.1, None) for i in range(15)]
        out = long_short_ratio_trend(rows, lookback=12)
        # indice 12: (1+1.2)/(1+0) - 1 = 2.2/1.0 - 1 = 1.2
        self.assertAlmostEqual(out[12], 1.2)

    def test_falls_back_to_toptrader_ratio(self):
        rows = [OiPoint(i, None, None, 2.0 + i * 0.1) for i in range(15)]
        out = long_short_ratio_trend(rows, lookback=12)
        self.assertAlmostEqual(out[12], 1.2 / 2.0)


class TestLiquidationClusterProximity(unittest.TestCase):
    def test_finds_nearest_below_and_above(self):
        levels = [
            LiquidationLevel(price=95.0, side="long", leverage=10, notional_usd=1000.0),
            LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=5000.0),
            LiquidationLevel(price=105.0, side="short", leverage=10, notional_usd=2000.0),
            LiquidationLevel(price=110.0, side="short", leverage=10, notional_usd=9000.0),
        ]
        result = liquidation_cluster_proximity(100.0, levels, bin_size=1.0)
        below = result["long_cluster_below"]
        above = result["short_cluster_above"]
        self.assertAlmostEqual(below["distance_pct"], 0.05)  # (100-95)/100
        self.assertEqual(below["notional_usd"], 1000.0)
        self.assertAlmostEqual(above["distance_pct"], 0.05)  # (105-100)/100
        self.assertEqual(above["notional_usd"], 2000.0)

    def test_no_cluster_returns_none_distance(self):
        result = liquidation_cluster_proximity(100.0, [])
        self.assertIsNone(result["long_cluster_below"]["distance_pct"])
        self.assertEqual(result["long_cluster_below"]["notional_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
