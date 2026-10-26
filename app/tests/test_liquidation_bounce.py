import unittest

from backtest.data import RealLiquidation
from backtest.strategies.liquidation_bounce import (
    LiquidationBounceLong,
    LiquidationBounceShort,
    _cascade_notional,
    _target_r,
)


def _liq(side, qty, time_ms=0, price=100.0):
    return RealLiquidation(time_ms=time_ms, side=side, price=price, quote_qty_usd=qty)


class TestCascadeNotional(unittest.TestCase):
    def test_sums_only_the_matching_side(self):
        events = [_liq("long", 100_000.0), _liq("short", 999_000.0), _liq("long", 50_000.0)]
        self.assertAlmostEqual(_cascade_notional(events, "long"), 150_000.0)

    def test_empty_events_is_zero(self):
        self.assertEqual(_cascade_notional([], "long"), 0.0)


class TestTargetR(unittest.TestCase):
    def test_at_threshold_gives_the_minimum(self):
        r = _target_r(cascade_usd=200_000.0, min_qty=200_000.0, r_min=1.0, r_max=2.5, r_scale=0.75)
        self.assertAlmostEqual(r, 1.0)

    def test_grows_with_cascade_size_but_saturates_at_max(self):
        small = _target_r(220_000.0, 200_000.0, 1.0, 2.5, 0.75)
        large = _target_r(23_000_000.0, 200_000.0, 1.0, 2.5, 0.75)
        self.assertGreater(large, small)
        self.assertLessEqual(large, 2.5)

    def test_below_threshold_ratio_clamped_to_one_not_negative_log(self):
        r = _target_r(50_000.0, 200_000.0, 1.0, 2.5, 0.75)
        self.assertAlmostEqual(r, 1.0)


class TestLiquidationBounceLong(unittest.TestCase):
    def test_no_events_no_entry(self):
        strat = LiquidationBounceLong()
        self.assertIsNone(strat.on_bar(_FakeCtx(side="flat", events=[])))

    def test_cascade_below_threshold_no_entry(self):
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0)
        events = [_liq("long", 50_000.0)]
        self.assertIsNone(strat.on_bar(_FakeCtx(side="flat", events=events)))

    def test_wrong_side_events_ignored(self):
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0)
        events = [_liq("short", 5_000_000.0)]
        self.assertIsNone(strat.on_bar(_FakeCtx(side="flat", events=events)))

    def test_zero_atr_no_entry(self):
        # Sin ATR (calentamiento o dato insuficiente) no hay unidad con la
        # que medir SL/TP -- no se debe entrar con un stop de tamaño cero.
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0)
        events = [_liq("long", 500_000.0)]
        self.assertIsNone(strat.on_bar(_FakeCtx(side="flat", events=events, atr=0.0)))

    def test_considerable_cascade_enters_long_with_sl_below_and_tp_above(self):
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0, stop_atr_mult=2.0)
        events = [_liq("long", 500_000.0)]
        target = strat.on_bar(_FakeCtx(side="flat", events=events, close=100.0, atr=1.0))
        self.assertEqual(target.side, "long")
        self.assertAlmostEqual(target.stop_loss_price, 98.0)  # 100 - 2*1.0
        self.assertGreater(target.take_profit_price, 100.0)

    def test_bigger_cascade_gets_a_wider_take_profit(self):
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0, stop_atr_mult=2.0)
        small = strat.on_bar(_FakeCtx(side="flat", events=[_liq("long", 210_000.0)], close=100.0, atr=1.0))
        large = strat.on_bar(_FakeCtx(side="flat", events=[_liq("long", 20_000_000.0)], close=100.0, atr=1.0))
        self.assertGreater(large.take_profit_price, small.take_profit_price)

    def test_wider_atr_widens_both_sl_and_tp(self):
        # La pregunta del usuario: "¿se puede hacer lo mismo con el TP?" --
        # si el ATR sube (mas volatilidad), el SL y el TP deben ensancharse
        # juntos (mismo target_r, mas unidades de ATR de por medio).
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0, stop_atr_mult=2.0)
        events = [_liq("long", 500_000.0)]
        calm = strat.on_bar(_FakeCtx(side="flat", events=events, close=100.0, atr=1.0))
        volatile = strat.on_bar(_FakeCtx(side="flat", events=events, close=100.0, atr=3.0))
        self.assertLess(volatile.stop_loss_price, calm.stop_loss_price)
        self.assertGreater(volatile.take_profit_price, calm.take_profit_price)

    def test_no_entry_while_already_in_a_position(self):
        strat = LiquidationBounceLong(min_quote_qty_usd=200_000.0)
        events = [_liq("long", 5_000_000.0)]
        self.assertIsNone(strat.on_bar(_FakeCtx(side="long", events=events)))


class TestLiquidationBounceShort(unittest.TestCase):
    def test_considerable_cascade_enters_short_with_sl_above_and_tp_below(self):
        strat = LiquidationBounceShort(min_quote_qty_usd=200_000.0, stop_atr_mult=2.0)
        events = [_liq("short", 500_000.0)]
        target = strat.on_bar(_FakeCtx(side="flat", events=events, close=100.0, atr=1.0))
        self.assertEqual(target.side, "short")
        self.assertAlmostEqual(target.stop_loss_price, 102.0)  # 100 + 2*1.0
        self.assertLess(target.take_profit_price, 100.0)


class _FakeBar:
    def __init__(self, close):
        self.close = close


class _FakePosition:
    def __init__(self, side):
        self.side = side


class _FakeCtx:
    """Doble minimo de `BacktestContext` -- solo los campos que
    `on_bar` de esta estrategia realmente lee."""

    def __init__(self, side, events, close=100.0, atr=1.0):
        self.position = _FakePosition(side)
        self.bar = _FakeBar(close)
        self.indicators = {"real_liquidations_this_bar": events, "atr": atr}


if __name__ == "__main__":
    unittest.main()
