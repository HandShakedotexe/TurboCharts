import unittest

from backtest.strategies.frvp_absorption import FrvpAbsorptionLong, FrvpAbsorptionShort


class _FakeBar:
    def __init__(self, low, high, close):
        self.low = low
        self.high = high
        self.close = close


class _FakePosition:
    def __init__(self, side):
        self.side = side


class _FakeProfile:
    """Mismo interfaz que volume_profile.SessionProfile, sin construir un
    perfil real de 24 buckets -- la estrategia solo usa estas tres
    propiedades."""

    def __init__(self, val=90.0, poc=100.0, vah=110.0):
        self.val = val
        self.poc = poc
        self.vah = vah


class _FakeCtx:
    def __init__(self, side, low, high, close, profile, bar_delta_zscore=0.0):
        self.position = _FakePosition(side)
        self.bar = _FakeBar(low, high, close)
        self.indicators = {"session_profile": profile, "bar_delta_zscore": bar_delta_zscore}


class TestFrvpAbsorptionLong(unittest.TestCase):
    def test_no_signal_when_there_is_no_profile_yet(self):
        strat = FrvpAbsorptionLong()
        ctx = _FakeCtx("flat", 88.0, 92.0, 91.0, None, bar_delta_zscore=-3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_no_wick_reaches_val_or_poc(self):
        strat = FrvpAbsorptionLong()
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 95.0, 97.0, 96.0, profile, bar_delta_zscore=-3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_delta_is_not_negative_enough(self):
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 88.0, 92.0, 91.0, profile, bar_delta_zscore=-1.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_close_did_not_hold_the_level(self):
        """La mecha toco VAL pero el cierre confirmo la ruptura hacia
        abajo (no hubo absorcion real) -- se descarta."""
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 88.0, 90.5, 89.0, profile, bar_delta_zscore=-3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_enters_long_at_val_with_sl_at_the_low_and_tp_at_the_poc(self):
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 88.0, 92.0, 91.0, profile, bar_delta_zscore=-2.5)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 88.0)
        self.assertAlmostEqual(target.take_profit_price, 100.0)

    def test_enters_long_at_poc_with_tp_at_the_vah(self):
        """Cuando VAL no fue tocado pero si el POC, el TP es el borde
        OPUESTO del rango -- no hay un nivel "mas hacia el centro" que el
        propio POC."""
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 98.0, 103.0, 101.0, profile, bar_delta_zscore=-2.5)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 98.0)
        self.assertAlmostEqual(target.take_profit_price, 110.0)

    def test_no_signal_when_target_is_not_above_entry(self):
        """Perfil degenerado (VAH == POC): el TP calculado para una entrada
        en el POC coincidiria con el propio nivel de entrada -- sin
        objetivo real, se descarta en vez de dar una señal sin sentido."""
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=100.0)
        ctx = _FakeCtx("flat", 98.0, 101.0, 100.0, profile, bar_delta_zscore=-2.5)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_the_absorption_candle_has_no_wick(self):
        """Sin distancia entre el cierre y el minimo, el SL coincidiria con
        la entrada -- se descarta en vez de un stop de distancia cero."""
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 90.0, 90.0, 90.0, profile, bar_delta_zscore=-2.5)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_entry_while_already_in_a_position(self):
        strat = FrvpAbsorptionLong(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("long", 88.0, 92.0, 91.0, profile, bar_delta_zscore=-2.5)
        self.assertIsNone(strat.on_bar(ctx))


class TestFrvpAbsorptionShort(unittest.TestCase):
    def test_no_signal_when_no_wick_reaches_vah_or_poc(self):
        strat = FrvpAbsorptionShort()
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 96.0, 97.0, 96.5, profile, bar_delta_zscore=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_delta_is_not_positive_enough(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 108.0, 112.0, 109.0, profile, bar_delta_zscore=1.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_close_did_not_hold_the_level(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 109.5, 112.0, 111.0, profile, bar_delta_zscore=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_enters_short_at_vah_with_sl_at_the_high_and_tp_at_the_poc(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 108.0, 112.0, 109.0, profile, bar_delta_zscore=2.5)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 112.0)
        self.assertAlmostEqual(target.take_profit_price, 100.0)

    def test_enters_short_at_poc_with_tp_at_the_val(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 97.0, 102.0, 99.0, profile, bar_delta_zscore=2.5)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 102.0)
        self.assertAlmostEqual(target.take_profit_price, 90.0)

    def test_no_signal_when_target_is_not_below_entry(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=100.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("flat", 97.0, 102.0, 100.0, profile, bar_delta_zscore=2.5)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_entry_while_already_in_a_position(self):
        strat = FrvpAbsorptionShort(delta_zscore_threshold=2.0)
        profile = _FakeProfile(val=90.0, poc=100.0, vah=110.0)
        ctx = _FakeCtx("short", 108.0, 112.0, 109.0, profile, bar_delta_zscore=2.5)
        self.assertIsNone(strat.on_bar(ctx))


if __name__ == "__main__":
    unittest.main()
