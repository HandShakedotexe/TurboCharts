"""Sin DB: el bot analista del paper trading (api/paper_analyst.py). Los tests
fijan las tres reglas que pidio el usuario: no se activa antes de tiempo,
agrupa operaciones parecidas, y NO pierde de vista una ganadora con una
logica distinta."""
import unittest

from api.paper_analyst import analyze, pair_similarity, tokenize

H = 3_600_000


def trade(i, note, *, side="long", pnl=5.0, r=1.0, tags=(), hour=15, interval="5m", sl=98.0, tp=104.0, **extra):
    t = {
        "trade_id": i, "status": "closed", "side": side, "symbol": "BTCUSDT", "interval": interval,
        "entry_price": 100.0, "sl_price": sl if side == "long" else 102.0, "tp_price": tp if side == "long" else 96.0,
        "net_pnl": pnl, "r_multiple": r, "note": note, "tags": list(tags),
        "filled_at_ms": hour * H, "created_ms": hour * H, "review_note": None,
        "followed_plan": None, "confidence": None,
    }
    t.update(extra)
    return t


SWEEP = "entre en largo tras el barrido de liquidez del pool inferior, rebote en la zona de liquidaciones"
BREAKOUT = "ruptura de resistencia con volumen alto, siga la tendencia alcista del dia"


class TestTokenize(unittest.TestCase):
    def test_strips_accents_stopwords_and_plurals(self):
        self.assertEqual(tokenize("Los pools de liquidación"), ["pool", "liquidacion"])

    def test_numbers_and_short_words_are_dropped(self):
        self.assertEqual(tokenize("el 100 de la ya"), [])


class TestActivation(unittest.TestCase):
    def test_waits_until_min_trades(self):
        out = analyze([trade(i, SWEEP) for i in range(1, 6)], min_trades=20)
        self.assertEqual(out["status"], "waiting")
        self.assertEqual(out["closed_trades"], 5)

    def test_force_runs_anyway_and_says_so(self):
        out = analyze([trade(i, SWEEP) for i in range(1, 6)], min_trades=20, force=True)
        self.assertEqual(out["status"], "ready")
        self.assertTrue(out["forced"])

    def test_pending_and_open_trades_do_not_count(self):
        trades = [trade(i, SWEEP) for i in range(1, 4)]
        trades.append({**trade(9, SWEEP), "status": "open", "net_pnl": None})
        self.assertEqual(analyze(trades, min_trades=4)["status"], "waiting")


class TestGrouping(unittest.TestCase):
    def test_similar_trades_group_together_and_apart_from_a_different_logic(self):
        # trades 2, 7, 10, 15 comparten relato y estructura (el ejemplo del usuario)
        similar_ids = [2, 7, 10, 15]
        trades = [trade(i, SWEEP, tags=["barrido"]) for i in similar_ids]
        trades += [trade(i, BREAKOUT, tags=["ruptura"], side="short", hour=3) for i in [3, 5, 8, 12]]
        out = analyze(trades, min_trades=8)
        groups = sorted(out["groups"], key=lambda g: g["trade_ids"])
        self.assertEqual([g["trade_ids"] for g in groups], [[2, 7, 10, 15], [3, 5, 8, 12]])
        self.assertIn("barrido", groups[0]["top_terms"])

    def test_structure_alone_is_not_enough_to_group(self):
        # Mismo lado/temporalidad/sesion/R:R pero relatos sin nada en comun
        a = trade(1, "aguacate guitarra montana")
        b = trade(2, "zapato helicoptero nublado")
        sim_text = pair_similarity(
            {"side": "long", "symbol": "BTCUSDT", "interval": "5m", "session": "nueva_york", "rr_bucket": "2-3"},
            {"side": "long", "symbol": "BTCUSDT", "interval": "5m", "session": "nueva_york", "rr_bucket": "2-3"},
            {"aguacate": 1.0}, {"zapato": 1.0},
        )
        self.assertLess(sim_text, 0.45)
        out = analyze([a, b], min_trades=2)
        self.assertEqual(out["groups"], [])
        self.assertEqual(len(out["unique_cases"]), 2)

    def test_opposite_sides_are_penalised(self):
        f = {"side": "long", "symbol": "BTCUSDT", "interval": "5m", "session": "asia", "rr_bucket": "2-3"}
        g = {**f, "side": "short"}
        v = {"barrido": 1.0}
        self.assertLess(pair_similarity(f, g, v, v), pair_similarity(f, f, v, v) * 0.6)


class TestUniqueWinner(unittest.TestCase):
    def test_a_lone_winner_with_different_logic_is_kept_and_flagged(self):
        trades = [trade(i, SWEEP, pnl=-5.0, r=-1.0) for i in range(1, 7)]
        trades.append(trade(7, "scalp contra la tendencia tras noticia de la FED", side="short", pnl=12.0, r=2.4, hour=3, interval="1h"))
        out = analyze(trades, min_trades=7)
        self.assertEqual(len(out["unique_cases"]), 1)
        case = out["unique_cases"][0]
        self.assertEqual(case["trade_id"], 7)
        self.assertTrue(case["winner"])

    def test_winners_are_listed_before_losers(self):
        trades = [trade(i, SWEEP) for i in range(1, 5)]
        trades.append(trade(8, "xilofono quimera", side="short", pnl=-3.0, r=-1.0, hour=2, interval="1h"))
        trades.append(trade(9, "ñandu tempestad", side="short", pnl=9.0, r=2.0, hour=4, interval="4h"))
        out = analyze(trades, min_trades=6)
        self.assertEqual([c["trade_id"] for c in out["unique_cases"]], [9, 8])


class TestVerdicts(unittest.TestCase):
    def test_small_group_gets_no_verdict(self):
        out = analyze([trade(i, SWEEP) for i in range(1, 4)], min_trades=3)
        self.assertEqual(out["groups"][0]["verdict"], "pocos_datos")

    def test_big_losing_group_is_flagged_to_avoid(self):
        out = analyze([trade(i, SWEEP, pnl=-5.0, r=-1.0) for i in range(1, 8)], min_trades=7)
        self.assertEqual(out["groups"][0]["verdict"], "evitar")

    def test_big_winning_group_is_a_candidate(self):
        trades = [trade(i, SWEEP, pnl=5.0, r=1.0) for i in range(1, 6)] + [trade(6, SWEEP, pnl=-5.0, r=-1.0)]
        out = analyze(trades, min_trades=6)
        self.assertEqual(out["groups"][0]["verdict"], "candidata")


class TestInsights(unittest.TestCase):
    def test_discipline_insight(self):
        trades = [trade(i, SWEEP, r=1.5, followed_plan=True) for i in range(1, 4)]
        trades += [trade(i, SWEEP, r=-1.0, pnl=-5.0, followed_plan=False) for i in range(4, 7)]
        out = analyze(trades, min_trades=6)
        self.assertTrue(any("Disciplina" in s for s in out["insights"]))


if __name__ == "__main__":
    unittest.main()
