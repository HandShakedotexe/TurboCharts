import unittest

from api.heatmap_model import MmrTable, MmrTier
from backtest.data import Bar, BigTrade, FundingPoint, OiPoint
from backtest.screen import (
    FeatureResult,
    build_feature_set,
    build_liquidation_cluster_features,
    forward_return,
    screen_liquidation_cluster,
    screen_symbol,
    summarize_by_feature,
)


def _bars(closes, start_ms=0, step_ms=3_600_000):
    return [
        Bar(start_ms + i * step_ms, c, c, c, c, 100.0, 50.0)
        for i, c in enumerate(closes)
    ]


class TestForwardReturn(unittest.TestCase):
    def test_hand_computed(self):
        bars = _bars([100.0, 110.0, 121.0])
        fwd = forward_return(bars, horizon=1)
        self.assertAlmostEqual(fwd[0], 0.10)
        self.assertAlmostEqual(fwd[1], 0.10)

    def test_tail_is_none_no_future_data(self):
        bars = _bars([100.0, 110.0, 121.0])
        fwd = forward_return(bars, horizon=1)
        self.assertIsNone(fwd[-1])

    def test_horizon_beyond_series_is_all_none(self):
        bars = _bars([100.0, 110.0])
        fwd = forward_return(bars, horizon=5)
        self.assertTrue(all(v is None for v in fwd))


class TestBuildFeatureSet(unittest.TestCase):
    def test_all_series_same_length_as_bars(self):
        bars = _bars([100.0 + i for i in range(200)])
        funding = [FundingPoint(i * 8 * 3_600_000, 0.0001) for i in range(30)]
        oi = [OiPoint(i * 3_600_000, 1000.0 + i, 1.0, 1.0) for i in range(200)]
        features = build_feature_set(bars, funding, oi)
        for name, series in features.items():
            self.assertEqual(len(series), len(bars), msg=f"{name} largo distinto de bars")


class TestScreenSymbol(unittest.TestCase):
    def test_regime_blocks_momentum_correlates_with_forward_return(self):
        # Un crecimiento EXPONENCIAL perfecto y constante (probado primero)
        # no sirve para este test: momentum_20 y el retorno futuro salen
        # practicamente CONSTANTES (mismo % cada barra), asi que el IC mide
        # ruido de punto flotante, no señal -- de ahi que el fixture use
        # bloques con tasa de crecimiento DISTINTA por bloque: momentum_20
        # (una vez calentado, dentro de cada bloque) y el retorno futuro a
        # horizonte corto deberian variar juntos, bloque a bloque.
        block_len, n_blocks = 100, 6
        growth_rates = [0.0000, 0.0005, 0.0010, 0.0015, 0.0020, 0.0025]
        closes = [100.0]
        for block in range(n_blocks):
            for _ in range(block_len):
                closes.append(closes[-1] * (1 + growth_rates[block]))
        bars = _bars(closes)
        funding = [FundingPoint(i * 8 * 3_600_000, 0.0001) for i in range(90)]
        oi = [OiPoint(i * 3_600_000, 1000.0 + i, 1.0, 1.0) for i in range(len(bars))]
        results = screen_symbol(bars, funding, oi, "TEST", "1h", horizons=(4,), min_n=100)
        by_feature = {r.feature: r.ic for r in results}
        self.assertGreater(by_feature["momentum_20"], 0.3)

    def test_below_min_n_returns_empty(self):
        bars = _bars([100.0] * 50)
        results = screen_symbol(bars, [], [], "TEST", "1h", min_n=500)
        self.assertEqual(results, [])


def _mmr_table():
    return MmrTable([MmrTier(0.0, 50_000.0, 0.004, 125)])


class TestBuildLiquidationClusterFeatures(unittest.TestCase):
    """La hipotesis REAL de pool de liquidaciones, via `engine._liquidation_cluster`
    reusado tal cual (nunca una version reescrita a mano), no los features
    de derivados genericos ya screeneados."""

    def test_all_series_same_length_as_bars_and_bounded(self):
        bars = _bars([100.0 + i * 0.01 for i in range(300)])
        big_trades = [
            BigTrade(time_ms=i * 3_600_000, price=100.0 + i * 0.01, notional_usd=1_000_000.0)
            for i in range(300)
        ]
        features = build_liquidation_cluster_features(bars, big_trades, _mmr_table())
        for name, series in features.items():
            self.assertEqual(len(series), len(bars), msg=f"{name} largo distinto de bars")
        # El imbalance es una fraccion firmada -- nunca deberia salirse de
        # [-1, 1] sin importar cuanto notional haya de cada lado.
        for v in features["liq_cluster_imbalance"]:
            self.assertGreaterEqual(v, -1.0)
            self.assertLessEqual(v, 1.0)

    def test_no_trades_yet_uses_sentinel_not_none(self):
        # Sin ningun big trade en la ventana de lookback: `_liquidation_cluster`
        # devuelve distance_pct=None en los dos lados -- debe salir como el
        # centinela 1.0, nunca None (rompe information_coefficient) ni 0.0
        # (significaria "el precio ya esta sobre el cluster").
        bars = _bars([100.0])
        features = build_liquidation_cluster_features(bars, [], _mmr_table())
        self.assertEqual(features["liq_long_cluster_distance_pct"][0], 1.0)
        self.assertEqual(features["liq_short_cluster_distance_pct"][0], 1.0)
        self.assertEqual(features["liq_cluster_imbalance"][0], 0.0)


class TestScreenLiquidationCluster(unittest.TestCase):
    def test_below_min_n_returns_empty(self):
        bars = _bars([100.0] * 50)
        results = screen_liquidation_cluster(bars, [], _mmr_table(), "BTCUSDT", "1h", min_n=200)
        self.assertEqual(results, [])

    def test_runs_without_crashing_on_synthetic_data(self):
        bars = _bars([100.0 + (i % 20) * 0.5 for i in range(300)])
        big_trades = [
            BigTrade(time_ms=i * 1_800_000, price=100.0 + (i % 40) * 0.25, notional_usd=500_000.0)
            for i in range(600)
        ]
        results = screen_liquidation_cluster(
            bars, big_trades, _mmr_table(), "BTCUSDT", "1h", horizons=(4,), min_n=200,
        )
        self.assertEqual(len(results), 5)  # 5 features x 1 horizonte
        for r in results:
            self.assertTrue(-1.0 <= r.ic <= 1.0)


class TestSummarizeByFeature(unittest.TestCase):
    def test_groups_and_ranks_by_median_abs_ic(self):
        results = [
            FeatureResult("feat_a", "BTCUSDT", "1h", 4, 1000, 0.20),
            FeatureResult("feat_a", "ETHUSDT", "1h", 4, 1000, 0.10),
            FeatureResult("feat_b", "BTCUSDT", "1h", 4, 1000, 0.01),
        ]
        summary = summarize_by_feature(results)
        self.assertEqual(summary[0]["feature"], "feat_a")
        self.assertEqual(summary[0]["n_symbols"], 2)
        self.assertAlmostEqual(summary[0]["median_ic"], 0.15)


if __name__ == "__main__":
    unittest.main()
