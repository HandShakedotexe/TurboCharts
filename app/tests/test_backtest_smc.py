import unittest

from backtest.data import Bar
from backtest.smc import fair_value_gaps, order_blocks, structure_events, swing_points


def _bar(t, open_, high, low, close):
    return Bar(t, open_, high, low, close, 0.0, 0.0)


class TestSwingPoints(unittest.TestCase):
    def test_finds_swing_high_and_low_with_lookback_one(self):
        bars = [
            _bar(0, 10, 10, 9.0, 10),
            _bar(1, 10, 12, 9.2, 11),   # swing high (window [0,1,2])
            _bar(2, 11, 11, 8.0, 9),    # swing low (window [1,2,3])
            _bar(3, 9, 13, 8.5, 12),    # TAMBIEN swing high (window [2,3,4]): high=13 unico
            _bar(4, 12, 12, 9.3, 10),
        ]
        points = swing_points(bars, lookback=1)
        self.assertEqual(len(points), 3)
        self.assertEqual((points[0].index, points[0].price, points[0].kind, points[0].confirmed_at_index),
                          (1, 12, "high", 2))
        self.assertEqual((points[1].index, points[1].price, points[1].kind, points[1].confirmed_at_index),
                          (2, 8.0, "low", 3))
        self.assertEqual((points[2].index, points[2].price, points[2].kind, points[2].confirmed_at_index),
                          (3, 13, "high", 4))

    def test_tie_in_window_is_not_a_unique_swing(self):
        # bars[1] y bars[2] comparten el low minimo DENTRO de la ventana
        # de bars[2] ([1,2,3]) -- ninguno de los dos es EL extremo unico,
        # asi que no cuenta ninguno.
        bars = [
            _bar(0, 10, 10, 10.0, 10),
            _bar(1, 10, 10, 5.0, 10),
            _bar(2, 10, 10, 5.0, 10),
            _bar(3, 10, 10, 8.0, 10),
            _bar(4, 10, 10, 10.0, 10),
        ]
        points = [p for p in swing_points(bars, lookback=1) if p.kind == "low"]
        self.assertEqual(points, [])

    def test_empty_bars_returns_empty(self):
        self.assertEqual(swing_points([], lookback=1), [])


class TestStructureEvents(unittest.TestCase):
    def test_single_bos_when_close_breaks_pending_swing_high(self):
        bars = [
            _bar(0, 9.8, 10, 9.0, 9.5),    # bajista (close<open) -- futuro Order Block
            _bar(1, 9.6, 12, 9.2, 11),      # swing high @1 (precio 12)
            _bar(2, 11, 11, 8.0, 9),         # swing low @2 (precio 8.0)
            _bar(3, 9, 13, 8.5, 12),          # swing high @3 (precio 13, no roto: close==12)
            _bar(4, 12, 12, 9.3, 10),
            _bar(5, 10, 14, 9.4, 13),         # close 13 > 12 -> rompe el swing high de @1
            _bar(6, 13, 13, 8.8, 8),
        ]
        events = structure_events(bars, lookback=1)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev.index, ev.kind, ev.direction, ev.level, ev.swing_index), (5, "BOS", "up", 12.0, 1))

    def test_no_events_when_price_never_breaks_a_swing(self):
        bars = [
            _bar(0, 10, 10, 9, 10),
            _bar(1, 10, 11, 9, 10),
            _bar(2, 10, 10, 8, 10),
            _bar(3, 10, 10, 9, 10),
            _bar(4, 10, 10, 9, 10),
        ]
        self.assertEqual(structure_events(bars, lookback=1), [])

    def test_choch_when_break_opposes_established_trend(self):
        # Primero un BOS hacia arriba fija trend="up"; el siguiente
        # rompimiento de un swing low (direccion contraria) debe marcarse
        # ChoCH, no BOS.
        bars = [
            _bar(0, 9.8, 10, 9.0, 9.5),
            _bar(1, 9.6, 12, 9.2, 11),
            _bar(2, 11, 11, 8.0, 9),
            _bar(3, 9, 13, 8.5, 12),
            _bar(4, 12, 12, 9.3, 10),
            _bar(5, 10, 14, 9.4, 13),   # BOS up (rompe el swing high @1, precio 12)
            _bar(6, 13, 13, 7.0, 6),     # cierra 6 < 8.0 -> rompe el swing low @2 -> ChoCH
        ]
        events = structure_events(bars, lookback=1)
        kinds = [(e.index, e.kind, e.direction) for e in events]
        self.assertIn((5, "BOS", "up"), kinds)
        self.assertIn((6, "ChoCH", "down"), kinds)


class TestOrderBlocks(unittest.TestCase):
    def test_bullish_order_block_before_bos_up_with_mitigation_and_break(self):
        bars = [
            _bar(0, 9.8, 10, 9.0, 9.5),    # bajista -- este es el Order Block
            _bar(1, 9.6, 12, 9.2, 11),
            _bar(2, 11, 11, 8.0, 9),
            _bar(3, 9, 13, 8.5, 12),
            _bar(4, 12, 12, 9.3, 10),
            _bar(5, 10, 14, 9.4, 13),   # BOS up, swing_index=1
            _bar(6, 13, 13, 8.8, 8),     # toca la zona del OB Y cierra por debajo -> mitigado Y breaker
        ]
        blocks = order_blocks(bars, lookback=1)
        self.assertEqual(len(blocks), 1)
        ob = blocks[0]
        self.assertEqual(ob.index, 0)
        self.assertEqual(ob.direction, "bullish")
        self.assertAlmostEqual(ob.top, 9.8)
        self.assertAlmostEqual(ob.bottom, 9.5)
        self.assertEqual(ob.impulse_event_index, 5)
        self.assertEqual(ob.mitigated_at_index, 6)
        self.assertEqual(ob.broken_at_index, 6)

    def test_no_order_blocks_without_structure_events(self):
        bars = [_bar(i, 10, 10, 9, 10) for i in range(5)]
        self.assertEqual(order_blocks(bars, lookback=1), [])


class TestFairValueGaps(unittest.TestCase):
    def test_bullish_gap_detected_and_later_filled(self):
        bars = [
            _bar(0, 9.5, 10, 9, 9.8),      # high=10
            _bar(1, 10, 15, 11, 14),        # impulso
            _bar(2, 14, 17, 13, 16),         # low=13 > bars[0].high(10) -> hueco alcista [10,13]
            _bar(3, 16, 12, 11, 11.5),        # toca la zona (low=11<=13, high=12>=10) -> se llena
        ]
        gaps = fair_value_gaps(bars)
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual((gap.start_index, gap.direction, gap.top, gap.bottom), (0, "up", 13, 10))
        self.assertEqual(gap.filled_at_index, 3)

    def test_bearish_gap_detected(self):
        bars = [
            _bar(0, 15, 16, 14, 15),   # low=14
            _bar(1, 14, 13, 9, 10),      # impulso bajista
            _bar(2, 10, 11, 8, 9),        # high=11 < bars[0].low(14) -> hueco bajista [14,11]
        ]
        gaps = fair_value_gaps(bars)
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual((gap.start_index, gap.direction, gap.top, gap.bottom), (0, "down", 14, 11))
        self.assertIsNone(gap.filled_at_index)

    def test_no_gap_when_candles_overlap(self):
        bars = [
            _bar(0, 10, 11, 9, 10),
            _bar(1, 10, 12, 9, 11),
            _bar(2, 11, 11, 9.5, 10),   # low(9.5) NO supera bars[0].high(11) -> sin hueco
        ]
        self.assertEqual(fair_value_gaps(bars), [])


if __name__ == "__main__":
    unittest.main()
