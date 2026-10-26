"""El cliente WebSocket es código de protocolo escrito a mano: se testea a fondo.

Se comprueba contra un servidor de pruebas independiente (tests/mockws.py) que
implementa el framing por su cuenta, para que un bug compartido no pase inadvertido.
"""
import asyncio
import json
import unittest

from liqrec.wsmin import WSClient, WSClosed, WSError

from .mockws import MockWSServer


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


class TestHandshake(unittest.TestCase):
    def test_connect_and_receive_text(self):
        async def scenario():
            async def on_open(conn):
                await conn.send_json({"hello": "world"})
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            msg = await ws.recv(timeout=5)
            await ws.close()
            await srv.stop()
            return msg
        self.assertEqual(json.loads(run(scenario())), {"hello": "world"})

    def test_rejects_bad_status(self):
        async def scenario():
            async def handle(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                with self.assertRaises(WSError):
                    await WSClient.connect(f"ws://127.0.0.1:{port}/")
            finally:
                server.close()
                await server.wait_closed()
        run(scenario())

    def test_rejects_wrong_accept_key(self):
        async def scenario():
            async def handle(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Accept: bogus=\r\n\r\n"
                )
                await writer.drain()
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                with self.assertRaises(WSError) as cm:
                    await WSClient.connect(f"ws://127.0.0.1:{port}/")
                self.assertIn("Accept", str(cm.exception))
            finally:
                server.close()
                await server.wait_closed()
        run(scenario())


class TestFraming(unittest.TestCase):
    def test_fragmented_message_is_reassembled(self):
        payload = json.dumps({"data": ["x" * 400, "y" * 400, "z" * 400]})

        async def scenario():
            async def on_open(conn):
                await conn.send_fragmented_text(payload, pieces=5)
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            got = await ws.recv(timeout=5)
            await ws.close()
            await srv.stop()
            return got
        self.assertEqual(run(scenario()), payload)

    def test_ping_from_server_is_answered_and_hidden(self):
        async def scenario():
            async def on_open(conn):
                await conn.send_ping(b"are-you-there")
                await asyncio.sleep(0.05)
                await conn.send_json({"after": "ping"})
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            # recv debe devolver el mensaje de datos, no el ping
            msg = await ws.recv(timeout=5)
            await asyncio.sleep(0.1)
            await ws.close()
            await srv.stop()
            return msg
        self.assertEqual(json.loads(run(scenario())), {"after": "ping"})

    def test_large_message_uses_64bit_length(self):
        big = json.dumps({"blob": "q" * 200_000})

        async def scenario():
            async def on_open(conn):
                await conn.send_text(big)
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            got = await ws.recv(timeout=10)
            await ws.close()
            await srv.stop()
            return got
        self.assertEqual(len(run(scenario())), len(big))

    def test_message_over_limit_is_rejected(self):
        async def scenario():
            async def on_open(conn):
                await conn.send_text("z" * 5000)
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url, max_message_bytes=1000)
            try:
                with self.assertRaises(WSError):
                    await ws.recv(timeout=5)
            finally:
                await ws.close()
                await srv.stop()
        run(scenario())

    def test_client_frames_are_masked(self):
        """mockws hace assert de que el cliente enmascara; aquí lo ejercitamos."""
        async def scenario():
            srv = await MockWSServer().start()
            ws = await WSClient.connect(srv.url)
            await ws.send(json.dumps({"op": "subscribe", "args": ["a" * 300]}))
            await asyncio.sleep(0.2)
            await ws.close()
            received = list(srv.received)
            await srv.stop()
            return received
        got = run(scenario())
        self.assertEqual(len(got), 1)
        self.assertEqual(json.loads(got[0])["op"], "subscribe")


class TestClose(unittest.TestCase):
    def test_server_close_raises_wsclosed(self):
        async def scenario():
            async def on_open(conn):
                await conn.send_close(1001, "going away")
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            try:
                with self.assertRaises(WSClosed) as cm:
                    await ws.recv(timeout=5)
                self.assertEqual(cm.exception.code, 1001)
            finally:
                await srv.stop()
        run(scenario())

    def test_abrupt_disconnect_raises(self):
        async def scenario():
            async def on_open(conn):
                await asyncio.sleep(0.05)
                await conn.close_hard()
            srv = await MockWSServer(on_open=on_open).start()
            ws = await WSClient.connect(srv.url)
            try:
                with self.assertRaises(WSClosed) as cm:
                    await ws.recv(timeout=5)
                self.assertEqual(cm.exception.code, 1006)
            finally:
                await ws.close()
                await srv.stop()
        run(scenario())


if __name__ == "__main__":
    unittest.main()
