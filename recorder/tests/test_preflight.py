"""El preflight tiene que validar LO QUE ESTÁ CONFIGURADO.

Un preflight que prueba la URL correcta mientras el grabador usa otra da luz
verde a un sistema roto. Es lo que pasó con el endpoint viejo de Binance: la
comprobación pasaba y el grabador llevaba dos días sin grabar nada.
"""
import asyncio
import io
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from liqrec import preflight
from liqrec.config import Config

BASE = {
    "recorder": {"data_dir": "./data"},
    "binance": {"ws_url": "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr"},
    "bybit": {}, "okx": {},
}
LOGGED = []


class _HcHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        LOGGED.append(self.path)
        code = 404 if "missing" in self.path else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    do_GET = do_POST

    def log_message(self, *a):
        pass


class TestStaleEndpointDetection(unittest.TestCase):
    def _report(self, cfg):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = preflight.check_config(cfg, Path("config.toml"))
        return ok, buf.getvalue()

    def test_current_binance_url_passes(self):
        ok, _ = self._report(Config.from_dict(BASE))
        self.assertTrue(ok)

    def test_decommissioned_binance_url_is_a_hard_failure(self):
        raw = {**BASE, "binance": {"ws_url": "wss://fstream.binance.com/stream?streams=!forceOrder@arr"}}
        ok, out = self._report(Config.from_dict(raw))
        self.assertFalse(ok, "la URL desmantelada tiene que bloquear el arranque")
        self.assertIn("2026-04-23", out)
        self.assertIn("/market/", out)

    def test_legacy_ws_path_also_caught(self):
        raw = {**BASE, "binance": {"ws_url": "wss://fstream.binance.com/ws/!forceOrder@arr"}}
        ok, _ = self._report(Config.from_dict(raw))
        self.assertFalse(ok)

    def test_disabled_exchange_is_not_checked(self):
        raw = {**BASE, "binance": {"enabled": False,
                                   "ws_url": "wss://fstream.binance.com/stream?streams=!forceOrder@arr"}}
        ok, out = self._report(Config.from_dict(raw))
        self.assertTrue(ok)
        self.assertIn("deshabilitado", out)


class TestControlStreamIsAdded(unittest.TestCase):
    def test_binance_control_stream_appended_to_configured_url(self):
        cfg = Config.from_dict(BASE)
        checks = preflight._configured_ws_checks(cfg)
        binance_url = checks[0][1]
        self.assertIn("!forceOrder@arr", binance_url)
        self.assertIn("aggTrade", binance_url, "sin control no se distingue calma de rotura")

    def test_control_not_duplicated_if_already_present(self):
        raw = {**BASE, "binance": {
            "ws_url": "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr/btcusdt@aggTrade"}}
        url = preflight._configured_ws_checks(Config.from_dict(raw))[0][1]
        self.assertEqual(url.count("aggTrade"), 1)

    def test_message_classifiers(self):
        self.assertEqual(preflight._kind_binance({"stream": "!forceOrder@arr"}), "target")
        self.assertEqual(preflight._kind_binance({"stream": "btcusdt@aggTrade"}), "control")
        self.assertEqual(preflight._kind_bybit({"topic": "allLiquidation.BTCUSDT"}), "target")
        self.assertEqual(preflight._kind_bybit({"topic": "publicTrade.BTCUSDT"}), "control")
        self.assertEqual(preflight._kind_okx({"arg": {"channel": "liquidation-orders"}}), "target")
        self.assertEqual(preflight._kind_okx({"arg": {"channel": "trades"}}), "control")
        self.assertEqual(preflight._kind_okx({}), "otro")


class TestHeartbeatCheck(unittest.TestCase):
    def setUp(self):
        LOGGED.clear()
        self.srv = HTTPServer(("127.0.0.1", 0), _HcHandler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _check(self, url):
        cfg = Config.from_dict({**BASE, "recorder": {"heartbeat_url": url}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = asyncio.run(preflight.check_heartbeat(cfg))
        return ok, buf.getvalue()

    def test_missing_url_warns_but_does_not_block(self):
        ok, out = self._check("")
        self.assertTrue(ok)
        self.assertIn("no se entera nadie", out)

    def test_valid_url_is_probed_with_log_not_a_success_ping(self):
        """Un ping normal marcaría 'todo bien' en el monitor mientras aquí no
        hay nada corriendo. /log registra el evento sin tocar el estado."""
        ok, out = self._check(f"{self.base}/07c0e59b")
        self.assertTrue(ok)
        self.assertEqual(LOGGED, ["/07c0e59b/log"])
        self.assertNotIn("/ping", "".join(LOGGED))

    def test_404_blocks(self):
        ok, out = self._check(f"{self.base}/missing")
        self.assertFalse(ok, "una URL de aviso inexistente tiene que bloquear")
        self.assertIn("404", out)

    def test_unreachable_url_warns_only(self):
        ok, out = self._check("http://127.0.0.1:9/nope")
        self.assertTrue(ok)
        self.assertIn("no se pudo contactar", out)


if __name__ == "__main__":
    unittest.main()
