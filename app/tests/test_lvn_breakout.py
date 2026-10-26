import unittest

from backtest.strategies.lvn_breakout import LvnBreakoutLong, LvnBreakoutShort


class _FakeBar:
    def __init__(self, close):
        self.close = close


class _FakePosition:
    def __init__(self, side):
        self.side = side


class _FakeProfile:
    """Mismo interfaz que volume_profile.SessionProfile, sin construir un
    perfil real de 24 buckets -- las estrategias solo usan estos cuatro
    metodos/propiedades."""

    def __init__(self, vah=110.0, val=90.0, hvn_above=None, hvn_below=None):
        self.vah = vah
        self.val = val
        self._hvn_above = hvn_above
        self._hvn_below = hvn_below

    def nearest_hvn_above(self, price):
        return self._hvn_above

    def nearest_hvn_below(self, price):
        return self._hvn_below


class _FakeCtx:
    def __init__(self, side, close, profile, cvd_change_20=0.0, volume_zscore_60=0.0, atr=1.0):
        self.position = _FakePosition(side)
        self.bar = _FakeBar(close)
        self.indicators = {
            "session_profile": profile, "cvd_change_20": cvd_change_20,
            "volume_zscore_60": volume_zscore_60, "atr": atr,
        }


class TestLvnBreakoutLong(unittest.TestCase):
    def test_no_signal_when_there_is_no_profile_yet(self):
        strat = LvnBreakoutLong()
        ctx = _FakeCtx("flat", 111.0, None, cvd_change_20=5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_price_has_not_broken_above_vah(self):
        strat = LvnBreakoutLong()
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("flat", 109.0, profile, cvd_change_20=5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_cvd_does_not_confirm(self):
        strat = LvnBreakoutLong()
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("flat", 111.0, profile, cvd_change_20=-1.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_volume_spike_is_below_threshold(self):
        strat = LvnBreakoutLong(volume_zscore_threshold=2.0)
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("flat", 111.0, profile, cvd_change_20=5.0, volume_zscore_60=1.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_there_is_no_hvn_above_to_target(self):
        """Sin objetivo estructural valido, la señal se descarta -- no se
        inventa un TP de respaldo (ver docstring de la estrategia)."""
        strat = LvnBreakoutLong()
        profile = _FakeProfile(vah=110.0, hvn_above=None)
        ctx = _FakeCtx("flat", 111.0, profile, cvd_change_20=5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_enters_long_with_sl_in_atr_and_tp_at_the_next_hvn(self):
        strat = LvnBreakoutLong(stop_atr_mult=2.0)
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("flat", 111.0, profile, cvd_change_20=5.0, volume_zscore_60=3.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 109.0)   # 111 - 2*1
        self.assertAlmostEqual(target.take_profit_price, 120.0)  # el HVN, no un multiplo de ATR

    def test_no_entry_while_already_in_a_position(self):
        strat = LvnBreakoutLong()
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("long", 111.0, profile, cvd_change_20=5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_without_atr(self):
        strat = LvnBreakoutLong()
        profile = _FakeProfile(vah=110.0, hvn_above=120.0)
        ctx = _FakeCtx("flat", 111.0, profile, cvd_change_20=5.0, volume_zscore_60=3.0, atr=0.0)
        self.assertIsNone(strat.on_bar(ctx))


class TestLvnBreakoutShort(unittest.TestCase):
    def test_no_signal_when_price_has_not_broken_below_val(self):
        strat = LvnBreakoutShort()
        profile = _FakeProfile(val=90.0, hvn_below=80.0)
        ctx = _FakeCtx("flat", 91.0, profile, cvd_change_20=-5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_cvd_does_not_confirm(self):
        strat = LvnBreakoutShort()
        profile = _FakeProfile(val=90.0, hvn_below=80.0)
        ctx = _FakeCtx("flat", 89.0, profile, cvd_change_20=1.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_no_signal_when_there_is_no_hvn_below_to_target(self):
        strat = LvnBreakoutShort()
        profile = _FakeProfile(val=90.0, hvn_below=None)
        ctx = _FakeCtx("flat", 89.0, profile, cvd_change_20=-5.0, volume_zscore_60=3.0)
        self.assertIsNone(strat.on_bar(ctx))

    def test_enters_short_with_sl_in_atr_and_tp_at_the_next_hvn(self):
        strat = LvnBreakoutShort(stop_atr_mult=2.0)
        profile = _FakeProfile(val=90.0, hvn_below=80.0)
        ctx = _FakeCtx("flat", 89.0, profile, cvd_change_20=-5.0, volume_zscore_60=3.0, atr=1.0)
        target = strat.on_bar(ctx)
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 91.0)   # 89 + 2*1
        self.assertAlmostEqual(target.take_profit_price, 80.0)  # el HVN


if __name__ == "__main__":
    unittest.main()
