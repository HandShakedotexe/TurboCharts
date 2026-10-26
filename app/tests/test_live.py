"""Sin red: se sustituye _get_json por un doble de prueba.

get_live_klines es una funcion normal de Python (FastAPI solo la envuelve
para leer query params); se puede llamar directo pasando los argumentos por
nombre, sin levantar un servidor. `start` siempre se pasa explicito (aunque
sea None): si se omite, el valor por defecto real del parametro es el
propio objeto `fastapi.Query(...)`, no `None` -- solo FastAPI lo resuelve a
None al recibir la peticion HTTP.
"""
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from api.routers import live

# Una fila real de /fapi/v1/klines (orden documentado):
# [openTime, open, high, low, close, volume, closeTime, ...]
SAMPLE_ROW = [1787943600000, "77500.0", "77900.0", "77300.0", "77546.4",
              "5437.362", 1787947199999, "0", 0, "0", "0", "0"]


class TestLiveKlines(unittest.TestCase):
    def test_time_uses_close_time_not_open_time(self):
        """Igual convencion que /api/candles (event_time = close_time):
        mezclarlas desalinearia la vela en vivo con el resto de la serie."""
        with patch.object(live, "_get_json", return_value=[SAMPLE_ROW]):
            rows = live.get_live_klines(symbol="BTCUSDT", interval="1h", limit=1, start=None)
        self.assertEqual(rows[0]["time"], 1787947199)

    def test_fields_are_parsed_as_floats(self):
        with patch.object(live, "_get_json", return_value=[SAMPLE_ROW]):
            rows = live.get_live_klines(symbol="BTCUSDT", interval="1h", limit=1, start=None)
        self.assertEqual(rows[0]["close"], 77546.4)
        self.assertEqual(rows[0]["volume"], 5437.362)

    def test_empty_response_yields_empty_list(self):
        with patch.object(live, "_get_json", return_value=[]):
            rows = live.get_live_klines(symbol="BTCUSDT", interval="1h", limit=1, start=None)
        self.assertEqual(rows, [])


class TestFetchSince(unittest.TestCase):
    def test_stops_when_a_page_is_not_full(self):
        """Una pagina con menos de 1500 filas es la ultima disponible: no
        hace falta seguir pidiendo."""
        with patch.object(live, "_get_json", return_value=[SAMPLE_ROW]):
            rows = live._fetch_since("BTCUSDT", "5m", start_ms=1787943600000)
        self.assertEqual(len(rows), 1)

    def test_pages_forward_until_caught_up_with_now(self):
        """Un hueco de un mes en 5m son ~8600 velas, mas de una pagina de
        1500 -- si esto no paginara, se veria justo el hueco que se noto al
        probar la app: semanas de precio faltantes en medio del grafico."""
        full_page = [SAMPLE_ROW] * 1500
        last_page = [SAMPLE_ROW] * 3
        calls = {"n": 0}

        def fake_get_json(url):
            calls["n"] += 1
            return full_page if calls["n"] < 3 else last_page

        with patch.object(live, "_get_json", side_effect=fake_get_json):
            rows = live._fetch_since("BTCUSDT", "5m", start_ms=1)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(rows), 1500 + 1500 + 3)

    def test_gives_up_after_max_pages_instead_of_looping_forever(self):
        full_page = [SAMPLE_ROW] * 1500
        with patch.object(live, "_get_json", return_value=full_page):
            rows = live._fetch_since("BTCUSDT", "5m", start_ms=1)
        self.assertEqual(len(rows), live.MAX_PAGES * 1500)


class TestGetJsonRetry(unittest.TestCase):
    """Bug real (2026-09-14, reporte del usuario con captura): un solo
    URLError transitorio contra Binance tumbaba /live-klines con HTTP 502
    sin forma de recuperarse sola -- reproducir la MISMA peticion segundos
    despues funcionaba (200 OK). `_get_json` reintenta 3 veces en total
    antes de rendirse."""

    def _urlopen_cm(self, body: bytes):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        return cm

    def test_succeeds_on_second_attempt_after_one_transient_failure(self):
        with patch.object(live.time, "sleep") as mock_sleep, \
             patch.object(
                 live.urllib.request, "urlopen",
                 side_effect=[urllib.error.URLError("timed out"), self._urlopen_cm(b"[1,2,3]")],
             ):
            result = live._get_json("http://x")
        self.assertEqual(result, [1, 2, 3])
        self.assertEqual(mock_sleep.call_count, 1)

    def test_raises_502_after_exhausting_all_retries(self):
        with patch.object(live.time, "sleep"), \
             patch.object(live.urllib.request, "urlopen", side_effect=urllib.error.URLError("timed out")) as mock_open:
            with self.assertRaises(HTTPException) as ctx:
                live._get_json("http://x")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(mock_open.call_count, 3)

    def test_no_retry_needed_makes_a_single_call(self):
        with patch.object(live.time, "sleep") as mock_sleep, \
             patch.object(live.urllib.request, "urlopen", return_value=self._urlopen_cm(b"[]")) as mock_open:
            result = live._get_json("http://x")
        self.assertEqual(result, [])
        self.assertEqual(mock_open.call_count, 1)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
