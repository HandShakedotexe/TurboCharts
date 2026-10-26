"""Sin red: se sustituye _get_json por un doble de prueba."""
import threading
import time
import unittest
from unittest.mock import patch

from api import bigtrades


def setUpModule():
    # El espaciado entre paginas (PAGE_PACE_SECONDS) evita un 429 real de
    # Binance -- irrelevante para lo que prueban estos tests y, sin
    # parchearlo, test_gives_up_after_max_pages tardaria MAX_PAGES * 0.2s
    # de verdad (30s) por una pausa que aqui no protege nada.
    global _sleep_patcher
    _sleep_patcher = patch("time.sleep")
    _sleep_patcher.start()


def tearDownModule():
    _sleep_patcher.stop()


def _trade(agg_id, price, qty, t_ms):
    return {"a": agg_id, "p": str(price), "q": str(qty), "T": t_ms, "m": False}


class TestFetchRecentBigTrades(unittest.TestCase):
    def test_filters_by_threshold(self):
        now_ms = int(time.time() * 1000)
        page = [
            _trade(1, "100.0", "1000", now_ms),   # notional 100,000 -- grande
            _trade(2, "100.0", "1", now_ms),      # notional 100 -- pequeño
        ]
        with patch.object(bigtrades, "_get_json", return_value=page):
            trades = bigtrades.fetch_recent_big_trades("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(trades, [(100.0, 100_000.0, now_ms)])

    def test_stops_when_page_reaches_now(self):
        now_ms = int(time.time() * 1000)
        page = [_trade(1, "50000", "1", now_ms)]  # notional 50,000, ya en "ahora"
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return page

        with patch.object(bigtrades, "_get_json", side_effect=fake):
            bigtrades.fetch_recent_big_trades("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(calls["n"], 1, "una pagina que ya llega a 'ahora' no deberia pedir otra")

    def test_pages_forward_with_from_id_until_caught_up(self):
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 3600_000
        full_page = [_trade(i, "20000", "1", old_ms) for i in range(1000)]
        last_page = [_trade(2000, "20000", "1", now_ms)]
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            if calls["n"] == 1:
                self.assertIn("startTime=", url)
                return full_page
            self.assertIn("fromId=1000", url)  # ultimo aggId de la pagina anterior + 1
            return last_page

        with patch.object(bigtrades, "_get_json", side_effect=fake):
            trades = bigtrades.fetch_recent_big_trades("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(trades), 1001)

    def test_empty_page_stops_pagination(self):
        with patch.object(bigtrades, "_get_json", return_value=[]):
            trades = bigtrades.fetch_recent_big_trades("BTCUSDT")
        self.assertEqual(trades, [])

    def test_gives_up_after_max_pages(self):
        old_ms = 0  # nunca "alcanza" ahora
        full_page = [_trade(i, "20000", "1", old_ms) for i in range(1000)]
        with patch.object(bigtrades, "_get_json", return_value=full_page):
            trades = bigtrades.fetch_recent_big_trades("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(len(trades), bigtrades.MAX_PAGES * 1000)


class TestCache(unittest.TestCase):
    def setUp(self):
        bigtrades._buffers.clear()

    def test_second_call_within_ttl_does_not_refetch(self):
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            now_ms = int(time.time() * 1000)
            return [_trade(1, "50000", "1", now_ms)]  # ya en "ahora": una sola pagina

        with patch.object(bigtrades, "_get_json", side_effect=fake):
            bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)
            bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(calls["n"], 1)

    def test_refresh_after_ttl_is_incremental_not_a_full_backfill(self):
        """El punto entero del buffer: un dia de BTCUSDT no se debe volver a
        pedir entero cada minuto. El segundo refresco (tras vencer el TTL)
        tiene que pedir con `fromId`, no repetir `startTime`."""
        now_ms = int(time.time() * 1000)
        first_page = [_trade(1, "50000", "1", now_ms)]
        second_page = [_trade(2, "60000", "1", now_ms)]
        calls = {"urls": []}

        def fake(url):
            calls["urls"].append(url)
            return second_page if "fromId" in url else first_page

        with patch.object(bigtrades, "_get_json", side_effect=fake):
            first = bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)
            bigtrades._buffers["BTCUSDT"].last_refresh = 0.0  # forzar que venza el TTL
            second = bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)

        self.assertIn("startTime=", calls["urls"][0])
        self.assertIn("fromId=2", calls["urls"][1])  # aggId 1 (primera pagina) + 1
        self.assertEqual(first, [(50000.0, 50000.0, now_ms)])
        self.assertEqual(second, [(50000.0, 50000.0, now_ms), (60000.0, 60000.0, now_ms)])

    def test_old_trades_are_pruned_once_outside_the_lookback_window(self):
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - int(30 * 3600 * 1000)  # 30h atras, fuera de una ventana de 24h
        with patch.object(bigtrades, "_get_json", return_value=[_trade(1, "50000", "1", now_ms)]):
            bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000, lookback_hours=24)
        # se inyecta a mano un trade viejo en el buffer para probar la poda
        bigtrades._buffers["BTCUSDT"].trades.insert(0, (0, old_ms, 1.0, 99_999.0, False))
        bigtrades._buffers["BTCUSDT"].last_refresh = 0.0
        with patch.object(bigtrades, "_get_json", return_value=[_trade(2, "50000", "1", now_ms)]):
            result = bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000, lookback_hours=24)
        self.assertNotIn((1.0, 99_999.0, old_ms), result)

    def test_concurrent_calls_during_a_cold_cache_only_fetch_once(self):
        """Bug real: con la cache todavia vacia, varias peticiones que
        llegan casi a la vez (React StrictMode monta el heatmap dos veces,
        el poll del frontend se puede solapar) lanzaban cada una su propia
        paginacion completa en paralelo -- saturo el servidor y dio 502 en
        pruebas reales. El candado por simbolo hace que la segunda espere a
        la primera en vez de repetir el trabajo."""
        calls = {"n": 0}
        start_gate = threading.Event()
        now_ms = int(time.time() * 1000)

        def slow_fetch(url):
            start_gate.wait(timeout=2)  # todos los hilos entran "a la vez"
            calls["n"] += 1
            time.sleep(0.05)
            return [_trade(1, "50000", "1", now_ms)]

        results = []
        with patch.object(bigtrades, "_get_json", side_effect=slow_fetch):
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)
                    )
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            start_gate.set()
            for t in threads:
                t.join(timeout=2)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r == [(50000.0, 50000.0, now_ms)] for r in results))


class TestSideAwareCache(unittest.TestCase):
    """`get_recent_big_trades_with_side_cached` -- mismo buffer compartido
    que `get_recent_big_trades_cached`, pero sin descartar el campo "m" de
    Binance (is_buyer_maker), usado por BigTradesOverlay.tsx para pintar
    compras/ventas de distinto color."""

    def setUp(self):
        bigtrades._buffers.clear()

    def _trade_with_side(self, agg_id, price, qty, t_ms, is_buyer_maker):
        return {"a": agg_id, "p": str(price), "q": str(qty), "T": t_ms, "m": is_buyer_maker}

    def test_carries_is_buyer_maker_through(self):
        now_ms = int(time.time() * 1000)
        page = [
            self._trade_with_side(1, "50000", "1", now_ms, True),   # venta agresiva
            self._trade_with_side(2, "60000", "1", now_ms, False),  # compra agresiva
        ]
        with patch.object(bigtrades, "_get_json", return_value=page):
            trades = bigtrades.get_recent_big_trades_with_side_cached("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(
            trades,
            [(50000.0, 50000.0, now_ms, True), (60000.0, 60000.0, now_ms, False)],
        )

    def test_shares_the_buffer_with_the_side_less_variant(self):
        """Un solo refresco de red sirve a las dos funciones -- no hace
        falta pedirle nada de nuevo a Binance solo por pedir el lado."""
        now_ms = int(time.time() * 1000)
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return [self._trade_with_side(1, "50000", "1", now_ms, True)]

        with patch.object(bigtrades, "_get_json", side_effect=fake):
            bigtrades.get_recent_big_trades_cached("BTCUSDT", threshold_usd=10_000)
            bigtrades.get_recent_big_trades_with_side_cached("BTCUSDT", threshold_usd=10_000)
        self.assertEqual(calls["n"], 1)


class TestCombineTrades(unittest.TestCase):
    """api/routers/big_trades.py combina DB + cola en vivo con esta funcion
    pura -- separada del router para poder probarla sin una conexion real a
    TimescaleDB (mismo criterio que el resto del proyecto)."""

    def test_filters_by_start_ms_and_sorts_by_time(self):
        historical = [(100.0, 100_000.0, 1000), (101.0, 150_000.0, 3000)]
        live = [(102.0, 200_000.0, 2000)]
        result = bigtrades.combine_trades(historical, live, start_ms=1500, limit=10)
        self.assertEqual([t[2] for t in result], [2000, 3000])

    def test_keeps_the_most_recent_when_over_limit(self):
        trades = [(100.0, 100_000.0, t) for t in range(5)]
        result = bigtrades.combine_trades(trades, [], start_ms=0, limit=2)
        self.assertEqual([t[2] for t in result], [3, 4])

    def test_empty_inputs_return_empty(self):
        self.assertEqual(bigtrades.combine_trades([], [], start_ms=0, limit=10), [])

    def test_works_with_4_tuples_that_carry_a_side(self):
        """Generica sobre la forma de la tupla -- big_trades.py la usa tanto
        para el par (precio, notional, tiempo) sin lado como para el de 4
        con `is_buyer_maker`, ver api/routers/big_trades.py."""
        historical = [(100.0, 100_000.0, 1000, True)]
        live = [(101.0, 150_000.0, 2000, False)]
        result = bigtrades.combine_trades(historical, live, start_ms=0, limit=10)
        self.assertEqual(result, [(100.0, 100_000.0, 1000, True), (101.0, 150_000.0, 2000, False)])


if __name__ == "__main__":
    unittest.main()
