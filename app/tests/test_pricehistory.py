"""Sin red: se sustituye _get_json por un doble de prueba."""
import time
import unittest
from unittest.mock import patch

from api import pricehistory
from api.routers import live as live_module

# Fila real de /fapi/v1/klines (orden documentado):
# [openTime, open, high, low, close, volume, closeTime, ...]
SAMPLE_ROW = [1787943600000, "77500.0", "77900.0", "77300.0", "77546.4",
              "5437.362", 1787947199999, "0", 0, "0", "0", "0"]


class TestGetRecentHighLowCached(unittest.TestCase):
    def setUp(self):
        pricehistory._cache.clear()

    def test_extracts_close_time_high_low_in_order(self):
        with patch.object(pricehistory, "_get_json", return_value=[SAMPLE_ROW]):
            data = pricehistory.get_recent_high_low_cached("BTCUSDT")
        self.assertEqual(data, [(1787947199999, 77900.0, 77300.0)])

    def test_second_call_within_ttl_does_not_refetch(self):
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return [SAMPLE_ROW]

        with patch.object(pricehistory, "_get_json", side_effect=fake):
            pricehistory.get_recent_high_low_cached("BTCUSDT")
            pricehistory.get_recent_high_low_cached("BTCUSDT")
        self.assertEqual(calls["n"], 1)

    def test_refetches_after_ttl_expires(self):
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return [SAMPLE_ROW]

        with patch.object(pricehistory, "_get_json", side_effect=fake):
            pricehistory.get_recent_high_low_cached("BTCUSDT")
            pricehistory._cache["BTCUSDT"].fetched_at = 0.0  # forzar que venza el TTL
            pricehistory.get_recent_high_low_cached("BTCUSDT")
        self.assertEqual(calls["n"], 2)

    def test_empty_response_yields_empty_list(self):
        with patch.object(pricehistory, "_get_json", return_value=[]):
            data = pricehistory.get_recent_high_low_cached("BTCUSDT")
        self.assertEqual(data, [])


class TestGetExtendedHighLowCached(unittest.TestCase):
    """`_fetch_since` (reutilizado de routers/live.py, no reimplementado)
    resuelve su propio `_get_json` en el namespace de `live.py`, no en el
    de `pricehistory.py` -- por eso estos tests parchean `live_module`, a
    diferencia de los de arriba."""

    def setUp(self):
        pricehistory._extended_cache.clear()

    def test_extracts_close_time_high_low_in_order(self):
        with patch.object(live_module, "_get_json", return_value=[SAMPLE_ROW]):
            data = pricehistory.get_extended_high_low_cached("BTCUSDT")
        self.assertEqual(data, [(1787947199999, 77900.0, 77300.0)])

    def test_second_call_within_ttl_does_not_refetch(self):
        calls = {"n": 0}

        def fake(url):
            calls["n"] += 1
            return [SAMPLE_ROW]

        with patch.object(live_module, "_get_json", side_effect=fake):
            pricehistory.get_extended_high_low_cached("BTCUSDT")
            pricehistory.get_extended_high_low_cached("BTCUSDT")
        self.assertEqual(calls["n"], 1)

    def test_does_not_share_cache_slot_with_the_recent_window(self):
        """Bug facil de meter: si el cache extendido usara la misma clave
        (solo el simbolo) que `_cache`, un refresco del tramo reciente
        pisaria o leeria el dato del tramo historico por error."""
        with patch.object(pricehistory, "_get_json", return_value=[SAMPLE_ROW]):
            pricehistory.get_recent_high_low_cached("BTCUSDT")
        self.assertNotIn("BTCUSDT", pricehistory._extended_cache)


if __name__ == "__main__":
    unittest.main()
