"""Arranque y apagado completos contra exchanges y REST simulados.

Este es el test que contesta "¿esto arranca de verdad?": monta la configuración
real, levanta todos los feeds y pollers, comprueba que health.json refleja el
estado, y apaga comprobando que los ficheros quedan cerrados y comprimidos.
"""
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from liqrec.__main__ import build, main_async
from liqrec.config import Config

from .mockws import MockWSServer

FORCE_ORDER = {"stream": "!forceOrder@arr", "data": {
    "e": "forceOrder", "E": 1568014460893,
    "o": {"s": "BTCUSDT", "S": "SELL", "q": "0.014", "p": "9910", "ap": "9910",
          "X": "FILLED", "l": "0.014", "z": "0.014", "T": 1568014460893}}}


class _RestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "instruments-info" in self.path:
            body = {"result": {"list": [{"symbol": "BTCUSDT", "status": "Trading"}],
                               "nextPageCursor": ""}}
        elif "tickers" in self.path:
            body = {"result": {"list": [{"symbol": "BTCUSDT", "openInterest": "1",
                                         "fundingRate": "0.0001", "markPrice": "60000"}]}}
        elif "premiumIndex" in self.path:
            body = [{"symbol": "BTCUSDT", "markPrice": "60000", "lastFundingRate": "0.0001"}]
        elif "openInterest" in self.path:
            body = {"symbol": "BTCUSDT", "openInterest": "1234", "time": 1}
        elif "open-interest" in self.path:
            body = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "oi": "10"}]}
        elif "mark-price" in self.path:
            body = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "markPx": "60000"}]}
        elif "funding-rate" in self.path:
            body = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001"}]}
        elif "instruments" in self.path:
            body = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01"}]}
        elif "exchangeInfo" in self.path:
            body = {"symbols": [{"symbol": "BTCUSDT"}]}
        else:
            body = {"ok": True}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class TestBoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http = HTTPServer(("127.0.0.1", 0), _RestHandler)
        self.rest = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()

    def _cfg(self, ws_url: str) -> Config:
        return Config.from_dict({
            "recorder": {
                "data_dir": str(self.root),
                "fsync_seconds": 0.1,
                "compress_on_rotate": True,
                "heartbeat_seconds": 0.5,
                "health_file": str(self.root / "health.json"),
                "reconnect_min_seconds": 0.05,
                "reconnect_max_seconds": 0.2,
                "oi_poll_seconds": 0.5,
                "funding_poll_seconds": 0.5,
                "reference_refresh_seconds": 0.5,
                "top_symbols": ["BTCUSDT"],
            },
            "binance": {"ws_url": ws_url, "rest_url": self.rest},
            "bybit": {"ws_url": ws_url, "rest_url": self.rest},
            "okx": {"ws_url": ws_url, "rest_url": self.rest,
                    "poll_symbols": ["BTC-USDT-SWAP"]},
        })

    def test_build_wires_every_feed_and_poller(self):
        cfg = self._cfg("ws://127.0.0.1:1/ws")
        writers, feeds, pollers_ = build(cfg)
        self.assertEqual({f.name for f in feeds}, {"binance", "bybit", "okx"})
        names = {p.name for p in pollers_}
        self.assertIn("binance/premium_index", names)
        self.assertIn("binance/open_interest", names)
        self.assertIn("bybit/tickers", names)
        self.assertIn("okx/open_interest", names)
        self.assertIn("okx/funding_rate", names)
        self.assertIn("reference/instruments", names)
        for w in writers:
            w.close()

    def test_disabled_exchange_is_not_started(self):
        cfg = self._cfg("ws://127.0.0.1:1/ws")
        cfg.okx.enabled = False
        writers, feeds, _ = build(cfg)
        self.assertEqual({f.name for f in feeds}, {"binance", "bybit"})
        for w in writers:
            w.close()

    def test_full_boot_records_and_shuts_down_cleanly(self):
        async def on_message(conn, text):
            try:
                msg = json.loads(text)
            except ValueError:
                if text == "ping":
                    await conn.send_text("pong")
                return
            if msg.get("op") == "subscribe":
                await conn.send_json({"success": True, "op": "subscribe"})
                await conn.send_json({"topic": "allLiquidation.BTCUSDT", "type": "snapshot",
                                      "ts": 1, "data": [{"T": 1, "s": "BTCUSDT", "S": "Buy",
                                                         "v": "1", "p": "60000"}]})
            elif msg.get("op") == "ping":
                await conn.send_json({"success": True, "ret_msg": "pong", "op": "ping"})

        async def on_open(conn):
            await conn.send_json(FORCE_ORDER)

        async def scenario():
            srv = await MockWSServer(on_open=on_open, on_message=on_message).start()
            cfg = self._cfg(srv.url)
            stop = asyncio.Event()
            task = asyncio.create_task(main_async(cfg, None, stop))
            # esperar a que health.json diga que hay feeds arriba
            health = self.root / "health.json"
            for _ in range(100):
                if health.exists():
                    data = json.loads(health.read_text())
                    if any(f["state"] == "up" for f in data["feeds"]):
                        break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.2)   # dejar correr un ciclo de pollers
            stop.set()
            rc = await asyncio.wait_for(task, timeout=20)
            await srv.stop()
            return rc, json.loads(health.read_text())

        rc, health = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
        self.assertEqual(rc, 0, "el apagado debe ser limpio")
        self.assertEqual(len(health["feeds"]), 3)
        self.assertGreaterEqual(health["uptime_seconds"], 0)

        # se grabaron liquidaciones
        liq = list((self.root / "raw").rglob("liquidation/**/*.jsonl*"))
        self.assertTrue(liq, "debió escribirse al menos un fichero de liquidaciones")

        # y datos de los pollers REST
        for stream in ("premium_index", "tickers", "open_interest", "instruments"):
            found = list((self.root / "raw").rglob(f"{stream}/**/*.jsonl*"))
            self.assertTrue(found, f"el poller {stream} no escribió nada")

        # al apagar, todo queda comprimido: no debe sobrar ningún .tmp
        self.assertEqual(list((self.root / "raw").rglob("*.tmp")), [])
        for p in (self.root / "raw").rglob("*.jsonl*"):
            self.assertTrue(p.name.endswith(".gz"), f"quedó sin comprimir: {p}")


if __name__ == "__main__":
    unittest.main()
