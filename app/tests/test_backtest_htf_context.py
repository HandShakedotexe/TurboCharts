import unittest

from backtest.data import Bar
from backtest.htf_context import HtfContext, build_htf_context, trend_at, zone_signal_at
from backtest.smc import OrderBlock, StructureEvent


def _bar(t, open_, high, low, close):
    return Bar(t, open_, high, low, close, 0.0, 0.0)


# Mismo fixture que test_backtest_smc.py::TestOrderBlocks
# (test_bullish_order_block_before_bos_up_with_mitigation_and_break) --
# produce EXACTAMENTE un StructureEvent (index=5, "up") y un OrderBlock
# (index=0, bullish, [9.5,9.8], impulse_event_index=5, mitigated_at_index=6,
# broken_at_index=6), asi que sirve para probar trend_at/zone_signal_at
# juntos sobre datos reales de smc.py, no solo objetos fabricados a mano.
_BARS = [
    _bar(0, 9.8, 10, 9.0, 9.5),
    _bar(1, 9.6, 12, 9.2, 11),
    _bar(2, 11, 11, 8.0, 9),
    _bar(3, 9, 13, 8.5, 12),
    _bar(4, 12, 12, 9.3, 10),
    _bar(5, 10, 14, 9.4, 13),
    _bar(6, 13, 13, 8.8, 8),
]


class TestTrendAt(unittest.TestCase):
    def setUp(self):
        self.ctx = build_htf_context(_BARS, lookback=1)

    def test_none_before_first_event(self):
        self.assertIsNone(trend_at(self.ctx, 4))

    def test_direction_at_and_after_event_index(self):
        self.assertEqual(trend_at(self.ctx, 5), "up")
        self.assertEqual(trend_at(self.ctx, 6), "up")
        self.assertEqual(trend_at(self.ctx, 1000), "up")


class TestZoneSignalAt(unittest.TestCase):
    def setUp(self):
        self.ctx = build_htf_context(_BARS, lookback=1)

    def test_none_before_block_is_confirmed_by_its_impulse_event(self):
        # El bloque existe como VELA desde index=0, pero no queda
        # CONFIRMADO como Order Block hasta el BOS en index=5 -- consultar
        # en htf_idx=4 (antes del BOS) no debe verlo, aunque `htf_idx=4 >=
        # ob.index=0`. Esta es la regresion real del bug de look-ahead
        # encontrado al escribir este modulo (bisectar por `ob.index` en
        # vez de `impulse_event_index`).
        self.assertIsNone(zone_signal_at(self.ctx, 4, price=9.6))

    def test_bullish_once_confirmed_and_not_yet_broken(self):
        self.assertEqual(zone_signal_at(self.ctx, 5, price=9.6), "bullish")

    def test_none_once_broken(self):
        # broken_at_index=6 -- desde ahi el bloque ya no cuenta como zona
        # bullish activa (se convirtio en breaker, ver docstring de smc.py).
        self.assertIsNone(zone_signal_at(self.ctx, 6, price=9.6))

    def test_none_when_price_outside_zone(self):
        self.assertIsNone(zone_signal_at(self.ctx, 5, price=100.0))

    def test_most_recently_confirmed_block_wins_on_overlap(self):
        # Dos bloques bullish que se superponen en precio ([0,10] y
        # [2,8]) confirmados en distintos instantes -- el mas nuevo
        # (impulse_event_index=20) debe ganar sobre el mas viejo
        # (impulse_event_index=10) para un precio (5.0) que cae dentro de
        # los dos.
        old_ob = OrderBlock(index=1, direction="bullish", top=10.0, bottom=0.0, impulse_event_index=10)
        new_ob = OrderBlock(index=15, direction="bearish", top=8.0, bottom=2.0, impulse_event_index=20)
        ctx = HtfContext(
            events=[], event_indices=[],
            blocks=[old_ob, new_ob], block_indices=[10, 20],
        )
        self.assertEqual(zone_signal_at(ctx, 25, price=5.0), "bearish")


if __name__ == "__main__":
    unittest.main()
