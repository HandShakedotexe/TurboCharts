"""End-to-end contra exchanges simulados.

Se levanta un servidor WebSocket que habla como Binance, Bybit y OKX, se corre
el adapter REAL contra él, se le corta la conexión a propósito, y se comprueba
sobre el disco: qué se grabó, qué NO se grabó (los acks de control no son
datos), que hubo reconexión y que el hueco quedó anotado.
"""
import asyncio
import gzip
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from liqrec.adapters import BinanceAdapter, BybitAdapter, OkxAdapter
from liqrec.config import Config, ExchangeConfig
from liqrec.coverage import CoverageLog, scan_gaps
from liqrec.writer import RawWriter

from .mockws import MockWSServer

FORCE_ORDER = {
    "e": "forceOrder", "E": 1568014460893,
    "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "0.014",
          "p": "9910", "ap": "9910", "X": "FILLED", "l": "0.014", "z": "0.014",
          "T": 1568014460893},
}


def read_all(root: Path, exchange: str, stream: str = "liquidation"):
    out = []
    for p in sorted((root / "raw" / exchange / stream).rglob("*.jsonl*")):
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as fh:
            out += [json.loads(l) for l in fh if l.strip()]
    return out


def base_config(data_dir: Path) -> Config:
    return Config(
        data_dir=data_dir,
        fsync_seconds=0.1,
        compress_on_rotate=False,
        heartbeat_seconds=0.5,
        reconnect_min_seconds=0.05,
        reconnect_max_seconds=0.2,
        proactive_reconnect_seconds=3600,
    )


class AdapterHarness:
    """Monta writer + coverage + adapter y lo corre hasta que se cumple algo."""

    def __init__(self, cls, root: Path, ws_url: str, rest_url: str = "", poll_symbols=None):
        self.cfg = base_config(root)
        self.xcfg = ExchangeConfig(enabled=True, ws_url=ws_url, rest_url=rest_url,
                                   poll_symbols=poll_symbols or [])
        self.writer = RawWriter(root, cls.name, "liquidation",
                                fsync_seconds=0.1, compress_on_rotate=False)
        self.coverage = CoverageLog(root, cls.name, "liquidation")
        self.adapter = cls(self.cfg, self.xcfg, self.writer, self.coverage)

    async def run_until(self, predicate, timeout=15.0):
        task = asyncio.create_task(self.adapter.run())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while loop.time() < deadline:
                if predicate(self.adapter):
                    return True
                await asyncio.sleep(0.05)
            return False
        finally:
            self.adapter.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.writer.close()
            self.coverage.close()


class TestBinanceAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_liquidations_ignores_acks_and_survives_a_drop(self):
        state = {"drops": 0}

        async def on_open(conn):
            # ack de control del endpoint combinado: NO es un dato
            await conn.send_json({"result": None, "id": 1})
            for i in range(3):
                await conn.send_json({"stream": "!forceOrder@arr", "data": FORCE_ORDER})
                await asyncio.sleep(0.02)
            await conn.send_ping(b"hb")          # el cliente debe responder solo
            await asyncio.sleep(0.05)
            if state["drops"] == 0:
                state["drops"] += 1
                await conn.close_hard()          # caída de red a mitad de stream
                return
            for i in range(2):
                await conn.send_json({"stream": "!forceOrder@arr", "data": FORCE_ORDER})
                await asyncio.sleep(0.02)

        async def scenario():
            srv = await MockWSServer(on_open=on_open).start()
            h = AdapterHarness(BinanceAdapter, self.root, srv.url)
            ok = await h.run_until(lambda a: a.reconnects >= 1 and a.total_messages >= 5)
            await srv.stop()
            return ok, srv.connections

        ok, connections = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
        self.assertTrue(ok, "el adapter debió reconectar y seguir grabando")
        self.assertGreaterEqual(connections, 2, "debió abrirse una segunda conexión")

        recs = read_all(self.root, "binance")
        self.assertGreaterEqual(len(recs), 5)
        # el ack de control no puede haberse grabado como dato
        for r in recs:
            self.assertNotIn("result", r["raw"], "un ack de control no es un evento")
            self.assertEqual(r["raw"]["data"]["e"], "forceOrder")

        cov = next((self.root / "coverage/binance/liquidation").glob("*.jsonl"))
        gaps = scan_gaps(cov)
        self.assertGreaterEqual(len(gaps), 1, "la caída debe quedar registrada como hueco")


class TestOkxAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_subscribes_to_swap_channel_and_pong_is_not_data(self):
        sent = []

        async def on_message(conn, text):
            sent.append(text)
            if text == "ping":                       # OKX: texto plano, no JSON
                await conn.send_text("pong")
                return
            msg = json.loads(text)
            if msg.get("op") == "subscribe":
                await conn.send_json({"event": "subscribe",
                                      "arg": {"channel": "liquidation-orders", "instType": "SWAP"}})
                await conn.send_json({
                    "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
                    "data": [{"details": [{"bkLoss": "0", "bkPx": "1.057", "ccy": "",
                                           "posSide": "long", "side": "sell", "sz": "768",
                                           "ts": "1723892524781"}],
                              "instFamily": "DYDX-USDT", "instId": "DYDX-USDT-SWAP",
                              "instType": "SWAP", "uly": "DYDX-USDT"}]})

        async def scenario():
            srv = await MockWSServer(on_message=on_message).start()
            h = AdapterHarness(OkxAdapter, self.root, srv.url)
            ok = await h.run_until(lambda a: a.total_messages >= 1, timeout=10)
            await srv.stop()
            return ok

        self.assertTrue(asyncio.run(asyncio.wait_for(scenario(), timeout=25)))
        sub = json.loads(sent[0])
        self.assertEqual(sub["args"], [{"channel": "liquidation-orders", "instType": "SWAP"}])
        recs = read_all(self.root, "okx")
        self.assertEqual(len(recs), 1, "solo el mensaje de datos; ni el ack ni el pong")
        self.assertEqual(recs[0]["raw"]["data"][0]["instId"], "DYDX-USDT-SWAP")


class _InstrumentsHandler(BaseHTTPRequestHandler):
    payload = {"result": {"list": [{"symbol": f"SYM{i}USDT", "status": "Trading"} for i in range(23)],
                          "nextPageCursor": ""}}

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestBybitAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http = HTTPServer(("127.0.0.1", 0), _InstrumentsHandler)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()

    def test_enumerates_symbols_chunks_subscriptions_and_filters_control(self):
        sent = []

        async def on_message(conn, text):
            msg = json.loads(text)
            sent.append(msg)
            if msg.get("op") == "subscribe":
                await conn.send_json({"success": True, "op": "subscribe",
                                      "ret_msg": "", "conn_id": "x"})
                await conn.send_json({
                    "topic": f"allLiquidation.{msg['args'][0].split('.')[1]}",
                    "type": "snapshot", "ts": 1739502303204,
                    "data": [{"T": 1739502302929, "s": "ROSEUSDT", "S": "Buy",
                              "v": "20", "p": "0.04499"}]})
            elif msg.get("op") == "ping":
                await conn.send_json({"success": True, "ret_msg": "pong", "op": "ping"})

        async def scenario():
            srv = await MockWSServer(on_message=on_message).start()
            h = AdapterHarness(BybitAdapter, self.root, srv.url,
                               rest_url=f"http://127.0.0.1:{self.http_port}")
            ok = await h.run_until(lambda a: a.total_messages >= 3, timeout=15)
            await srv.stop()
            return ok, h.adapter.symbols

        ok, symbols = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
        self.assertTrue(ok)
        self.assertEqual(len(symbols), 23, "debe enumerar los instrumentos por REST")

        subs = [m for m in sent if m.get("op") == "subscribe"]
        self.assertEqual(len(subs), 3, "23 símbolos en trozos de 10 -> 3 mensajes")
        self.assertTrue(all(len(s["args"]) <= 10 for s in subs))
        self.assertTrue(all(a.startswith("allLiquidation.") for s in subs for a in s["args"]))

        recs = read_all(self.root, "bybit")
        self.assertGreaterEqual(len(recs), 3)
        for r in recs:
            self.assertIn("topic", r["raw"], "solo mensajes con topic; los acks se descartan")


if __name__ == "__main__":
    unittest.main()
