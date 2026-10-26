"""Servidor WebSocket mínimo para los tests.

Implementa el lado servidor del mismo protocolo (frames sin máscara hacia el
cliente, verificación de que el cliente sí enmascara). Deliberadamente NO
reutiliza el codificador de wsmin: si ambos lados compartieran el bug, el test
pasaría igualmente.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, n)
    elif n < (1 << 16):
        head = struct.pack("!BBH", b0, 126, n)
    else:
        head = struct.pack("!BBQ", b0, 127, n)
    return head + payload   # servidor -> cliente va sin máscara


class MockWSServer:
    """Servidor de pruebas. `on_open(conn)` recibe una conexión ya establecida."""

    def __init__(self, on_open=None, on_message=None):
        self.on_open = on_open
        self.on_message = on_message
        self.server = None
        self.port = None
        self.connections = 0
        self.received: list = []
        self._conns: list = []

    async def start(self, host="127.0.0.1", port=0):
        self.server = await asyncio.start_server(self._handle, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def stop(self):
        for c in list(self._conns):
            await c.close_hard()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        key = ""
        for line in lines[1:]:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        self.connections += 1
        conn = MockConn(self, reader, writer)
        self._conns.append(conn)
        try:
            if self.on_open:
                await self.on_open(conn)
            await conn.read_loop()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


class MockConn:
    def __init__(self, server: MockWSServer, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def send_text(self, s: str):
        self.writer.write(server_frame(0x1, s.encode()))
        await self.writer.drain()

    async def send_json(self, obj):
        await self.send_text(json.dumps(obj))

    async def send_fragmented_text(self, s: str, pieces: int = 3):
        data = s.encode()
        step = max(1, len(data) // pieces)
        parts = [data[i : i + step] for i in range(0, len(data), step)]
        for i, part in enumerate(parts):
            op = 0x1 if i == 0 else 0x0
            fin = i == len(parts) - 1
            self.writer.write(server_frame(op, part, fin=fin))
        await self.writer.drain()

    async def send_ping(self, payload: bytes = b"hb"):
        self.writer.write(server_frame(0x9, payload))
        await self.writer.drain()

    async def send_close(self, code: int = 1000, reason: str = ""):
        self.writer.write(server_frame(0x8, struct.pack("!H", code) + reason.encode()))
        await self.writer.drain()
        self.closed = True

    async def close_hard(self):
        """Corta el socket sin close handshake: simula una caída de red."""
        self.closed = True
        try:
            self.writer.transport.abort()
        except Exception:
            try:
                self.writer.close()
            except Exception:
                pass

    async def read_loop(self):
        while not self.closed:
            hdr = await self.reader.readexactly(2)
            b0, b1 = hdr
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            assert masked, "RFC 6455: el cliente DEBE enmascarar sus frames"
            mask = await self.reader.readexactly(4)
            data = await self.reader.readexactly(n) if n else b""
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 0x8:
                self.closed = True
                return
            if op == 0x9:            # ping del cliente -> pong
                self.writer.write(server_frame(0xA, data))
                await self.writer.drain()
                continue
            if op == 0xA:
                continue
            text = data.decode("utf-8", "replace")
            self.server.received.append(text)
            if self.server.on_message:
                await self.server.on_message(self, text)
