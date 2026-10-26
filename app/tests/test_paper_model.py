"""Sin DB ni red: dimensionado, simulacion contra velas y estadisticas del
paper trading (api/paper_model.py). Cada test de `simulate` existe porque
fija una decision de honestidad del simulador (ver el docstring del modulo):
borrarlo "por redundante" cambia en silencio cuanto parece ganar la cuenta
de papel."""
import unittest

from api.paper_model import (
    close_math, compute_summary, excursions, first_bar_ms, planned_rr, rr_bucket, session_for,
    simulate, size_position, validate_levels,
)

M = 60_000


def bar(i, o, h, l, c=None):
    """Vela de 1m numero `i` (open_ms = i minutos)."""
    return (i * M, o, h, l, o if c is None else c)


def pending_long(entry=100.0, sl=98.0, tp=104.0, last_checked_ms=1 * M):
    return {
        "status": "pending", "side": "long", "entry_price": entry, "sl_price": sl, "tp_price": tp,
        "fill_bar_ms": None, "filled_at_ms": None, "high_since_fill": None, "low_since_fill": None,
        "last_checked_ms": last_checked_ms,
    }


def open_long(entry=100.0, sl=98.0, tp=104.0, fill_bar=0):
    t = pending_long(entry, sl, tp, last_checked_ms=(fill_bar + 1) * M)
    t.update(status="open", fill_bar_ms=fill_bar * M, filled_at_ms=fill_bar * M,
             high_since_fill=entry, low_since_fill=entry)
    return t


class TestSizing(unittest.TestCase):
    def test_risk_pct_of_balance(self):
        s = size_position(100.0, 100.0, 98.0, risk_pct=5.0, risk_usd=None, max_leverage=50)
        self.assertAlmostEqual(s["risk_usd"], 5.0)
        self.assertAlmostEqual(s["qty"], 2.5)  # perder 2 USD por unidad x 2.5 = 5 USD
        self.assertFalse(s["capped"])

    def test_leverage_cap_reduces_qty_and_flags_it(self):
        # SL a 0.1% -> 5 USD de riesgo pediria 5000 USD de nocional con 100 de balance
        s = size_position(100.0, 100.0, 99.9, risk_pct=5.0, risk_usd=None, max_leverage=20)
        self.assertTrue(s["capped"])
        self.assertAlmostEqual(s["notional_usd"], 2000.0)
        self.assertLess(s["risk_usd"], 5.0)

    def test_explicit_risk_usd_wins_over_pct(self):
        s = size_position(100.0, 100.0, 98.0, risk_pct=5.0, risk_usd=1.0, max_leverage=50)
        self.assertAlmostEqual(s["risk_usd"], 1.0)

    def test_rejects_zero_distance_and_empty_balance(self):
        with self.assertRaises(ValueError):
            size_position(100.0, 100.0, 100.0, risk_pct=5.0, risk_usd=None, max_leverage=20)
        with self.assertRaises(ValueError):
            size_position(0.0, 100.0, 98.0, risk_pct=5.0, risk_usd=None, max_leverage=20)


class TestValidateLevels(unittest.TestCase):
    def test_long_and_short_orientation(self):
        validate_levels("long", 100, 98, 104)
        validate_levels("short", 100, 102, 96)
        with self.assertRaises(ValueError):
            validate_levels("long", 100, 102, 104)  # SL arriba de la entrada
        with self.assertRaises(ValueError):
            validate_levels("short", 100, 98, 96)   # SL debajo de la entrada


class TestHelpers(unittest.TestCase):
    def test_rr_and_buckets(self):
        self.assertAlmostEqual(planned_rr("long", 100, 98, 104), 2.0)
        self.assertEqual(rr_bucket(0.5), "<1")
        self.assertEqual(rr_bucket(2.0), "2-3")
        self.assertEqual(rr_bucket(3.5), ">=3")
        self.assertEqual(rr_bucket(None), "?")

    def test_sessions_by_utc_hour(self):
        self.assertEqual(session_for(3 * 3_600_000), "asia")
        self.assertEqual(session_for(9 * 3_600_000), "londres")
        self.assertEqual(session_for(15 * 3_600_000), "nueva_york")
        self.assertEqual(session_for(22 * 3_600_000), "cierre")

    def test_first_bar_is_the_next_minute(self):
        # La vela en curso trae movimiento previo a la orden: no se evalua.
        self.assertEqual(first_bar_ms(5 * M + 10_000), 6 * M)
        self.assertEqual(first_bar_ms(5 * M), 6 * M)

    def test_close_math_long_short_and_fees(self):
        c = close_math("long", 100.0, 104.0, 2.5, 0.04, 5.0)
        self.assertAlmostEqual(c["gross_pnl"], 10.0)
        self.assertAlmostEqual(c["fees"], (100 * 2.5 + 104 * 2.5) * 0.0004)
        self.assertAlmostEqual(c["r_multiple"], c["net_pnl"] / 5.0)
        s = close_math("short", 100.0, 96.0, 2.5, 0.0, 5.0)
        self.assertAlmostEqual(s["gross_pnl"], 10.0)

    def test_excursions_long_and_short(self):
        self.assertEqual(excursions("long", 100.0, 2.0, 106.0, 97.0), {"mfe_usd": 12.0, "mae_usd": 6.0})
        self.assertEqual(excursions("short", 100.0, 2.0, 103.0, 94.0), {"mfe_usd": 12.0, "mae_usd": 6.0})
        self.assertEqual(excursions("long", 100.0, 2.0, None, None), {"mfe_usd": None, "mae_usd": None})


class TestSimulatePending(unittest.TestCase):
    def test_no_touch_no_change_but_progress_is_recorded(self):
        out = simulate(pending_long(), [bar(1, 105, 106, 104), bar(2, 105, 106, 104)])
        self.assertNotIn("status", out)
        self.assertEqual(out["last_checked_ms"], 2 * M)

    def test_fills_when_price_touches_entry(self):
        out = simulate(pending_long(), [bar(1, 103, 104, 99.5)])
        self.assertEqual(out["status"], "open")
        self.assertEqual(out["filled_at_ms"], 1 * M)
        self.assertEqual(out["fill_bar_ms"], 1 * M)

    def test_tp_is_not_counted_on_the_fill_bar(self):
        # Esa vela toca la entrada (low<=100) y el TP (high>=104): no se sabe
        # el orden, asi que el TP no cuenta hasta la vela siguiente.
        out = simulate(pending_long(), [bar(1, 101, 105, 99.5)])
        self.assertEqual(out["status"], "open")
        self.assertNotIn("exit_price", out)

    def test_sl_is_counted_on_the_fill_bar_at_the_sl_price(self):
        out = simulate(pending_long(), [bar(1, 101, 102, 97)])
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["exit_reason"], "sl")
        self.assertEqual(out["exit_price"], 98.0)  # no el open: el open es anterior a la entrada

    def test_rerunning_the_same_bars_is_idempotent(self):
        t = pending_long()
        bars = [bar(1, 101, 105, 99.5), bar(2, 101, 102, 100.5)]
        first = simulate(t, bars)
        again = simulate({**t, **first}, bars)
        self.assertEqual(again.get("status", "open"), "open")
        self.assertEqual(again["fill_bar_ms"] if "fill_bar_ms" in again else first["fill_bar_ms"], 1 * M)


class TestSimulateOpen(unittest.TestCase):
    def test_take_profit(self):
        out = simulate(open_long(), [bar(1, 101, 104.5, 100.5)])
        self.assertEqual((out["status"], out["exit_reason"], out["exit_price"]), ("closed", "tp", 104.0))
        self.assertFalse(out["ambiguous_exit"])

    def test_stop_loss(self):
        out = simulate(open_long(), [bar(1, 99, 99.5, 97.5)])
        self.assertEqual((out["exit_reason"], out["exit_price"]), ("sl", 98.0))

    def test_same_bar_sl_and_tp_assumes_sl_and_flags_it(self):
        out = simulate(open_long(), [bar(1, 100, 105, 97)])
        self.assertEqual(out["exit_reason"], "sl")
        self.assertTrue(out["ambiguous_exit"])

    def test_gap_through_stop_exits_at_open_not_at_stop(self):
        out = simulate(open_long(), [bar(1, 96, 97, 95)])
        self.assertEqual(out["exit_price"], 96.0)

    def test_gap_through_target_exits_at_open(self):
        out = simulate(open_long(), [bar(1, 106, 107, 105)])
        self.assertEqual((out["exit_reason"], out["exit_price"]), ("tp", 106.0))

    def test_tracks_high_and_low_since_fill(self):
        out = simulate(open_long(), [bar(1, 100, 102, 99), bar(2, 101, 103, 98.5)])
        self.assertEqual(out["high_since_fill"], 103.0)
        self.assertEqual(out["low_since_fill"], 98.5)
        self.assertNotIn("status", out)

    def test_short_mirror(self):
        t = open_long()
        t.update(side="short", entry_price=100.0, sl_price=102.0, tp_price=96.0)
        out = simulate(t, [bar(1, 99, 99.5, 95.5)])
        self.assertEqual((out["exit_reason"], out["exit_price"]), ("tp", 96.0))

    def test_closed_or_cancelled_orders_are_left_alone(self):
        t = open_long()
        t["status"] = "closed"
        self.assertEqual(simulate(t, [bar(1, 99, 99.5, 90)]), {})


class TestSummary(unittest.TestCase):
    SETTINGS = {"initial_balance": 100.0}

    def closed(self, i, pnl, r):
        return {"status": "closed", "net_pnl": pnl, "r_multiple": r, "closed_at_ms": i * M, "filled_at_ms": i * M}

    def test_balance_stats_and_drawdown(self):
        trades = [self.closed(1, 10.0, 2.0), self.closed(2, -5.0, -1.0), self.closed(3, -5.0, -1.0), self.closed(4, 20.0, 4.0)]
        s = compute_summary(self.SETTINGS, trades)
        self.assertAlmostEqual(s["balance"], 120.0)
        self.assertAlmostEqual(s["return_pct"], 20.0)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["profit_factor"], 3.0)
        self.assertAlmostEqual(s["max_drawdown_usd"], 10.0)
        self.assertAlmostEqual(s["expectancy_r"], 1.0)
        self.assertEqual(len(s["equity_curve"]), 5)

    def test_open_and_pending_do_not_touch_balance(self):
        trades = [self.closed(1, 10.0, 2.0), {"status": "open"}, {"status": "pending"}]
        s = compute_summary(self.SETTINGS, trades)
        self.assertAlmostEqual(s["balance"], 110.0)
        self.assertEqual((s["open_trades"], s["pending_trades"]), (1, 1))

    def test_empty_account(self):
        s = compute_summary(self.SETTINGS, [])
        self.assertEqual(s["balance"], 100.0)
        self.assertIsNone(s["win_rate"])
        self.assertEqual(s["equity_curve"], [])


if __name__ == "__main__":
    unittest.main()
