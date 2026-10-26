"""El interruptor de hombre muerto: lo que importa es CUÁNDO calla."""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from liqrec.heartbeat import Heartbeat, _redact

PINGS = []


class _PingHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        PINGS.append(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def snap(states):
    return {"uptime_seconds": 42,
            "feeds": [{"exchange": e, "state": s, "total_messages": 1,
                       "reconnects": 0, "gaps": 0} for e, s in states]}


class TestHealthLogic(unittest.TestCase):
    def hb(self, **kw):
        return Heartbeat("http://x/ping", lambda: {}, **kw)

    def test_all_up_is_healthy(self):
        self.assertTrue(self.hb().is_healthy(snap([("binance", "up"), ("bybit", "up")])))

    def test_one_down_is_not_healthy_by_default(self):
        self.assertFalse(self.hb().is_healthy(snap([("binance", "up"), ("bybit", "down")])))

    def test_partial_can_be_accepted_when_configured(self):
        hb = self.hb(require_all_feeds=False)
        self.assertTrue(hb.is_healthy(snap([("binance", "up"), ("bybit", "down")])))
        self.assertFalse(hb.is_healthy(snap([("binance", "down")])))

    def test_no_feeds_is_never_healthy(self):
        self.assertFalse(self.hb().is_healthy({"feeds": []}))
        self.assertFalse(self.hb().is_healthy({}))

    def test_disabled_without_url(self):
        self.assertFalse(Heartbeat("", lambda: {}).enabled)
        self.assertFalse(Heartbeat("   ", lambda: {}).enabled)

    def test_token_is_redacted_in_logs(self):
        url = "https://hc-ping.com/2f4a9c1e-0000-1111-2222-333344445555"
        out = _redact(url)
        self.assertNotIn("333344445555", out)
        self.assertIn("…", out)


class TestPinging(unittest.TestCase):
    def setUp(self):
        PINGS.clear()
        self.srv = HTTPServer(("127.0.0.1", 0), _PingHandler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/ping"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _run(self, states, cycles=2):
        hb = Heartbeat(self.url, lambda: snap(states), interval=0.05)

        async def scenario():
            t = asyncio.create_task(hb.run())
            for _ in range(80):
                if hb.sent + hb.skipped >= cycles:
                    break
                await asyncio.sleep(0.02)
            hb.stop()
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        return hb

    def test_pings_while_healthy(self):
        hb = self._run([("binance", "up"), ("bybit", "up")])
        self.assertGreaterEqual(hb.sent, 2)
        self.assertEqual(hb.skipped, 0)
        self.assertGreaterEqual(len(PINGS), 2)
        self.assertEqual(PINGS[0]["uptime_seconds"], 42)

    def test_goes_silent_when_a_feed_is_down(self):
        """El aviso NO es un mensaje: es la ausencia de mensajes."""
        hb = self._run([("binance", "up"), ("bybit", "down")])
        self.assertGreaterEqual(hb.skipped, 2)
        self.assertEqual(hb.sent, 0)
        self.assertEqual(PINGS, [], "un estado no sano no debe generar ningún ping")

    def test_ping_failure_never_kills_the_recorder(self):
        hb = Heartbeat("http://127.0.0.1:9/nope", lambda: snap([("binance", "up")]),
                       interval=0.05, timeout=1)

        async def scenario():
            t = asyncio.create_task(hb.run())
            for _ in range(80):
                if hb.failed >= 1:
                    break
                await asyncio.sleep(0.02)
            hb.stop()
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        self.assertGreaterEqual(hb.failed, 1)
        self.assertIsNotNone(hb.last_error)   # se anota, no se propaga

    def test_disabled_heartbeat_exits_immediately(self):
        hb = Heartbeat("", lambda: snap([("binance", "up")]), interval=999)
        asyncio.run(asyncio.wait_for(hb.run(), timeout=5))
        self.assertEqual(hb.sent, 0)


if __name__ == "__main__":
    unittest.main()
