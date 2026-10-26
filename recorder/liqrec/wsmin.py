"""Cliente WebSocket mínimo (RFC 6455) sobre asyncio, solo stdlib.

Por qué no usar `websockets` o `aiohttp`: el recorder es el único servicio del
proyecto que no puede caerse, y cada dependencia es una forma más de que se
caiga (una release que rompe la API, una rueda que no compila en el VPS, un
`pip install` que falla a las 3 de la mañana). El subconjunto del protocolo que
necesitamos —cliente, TLS, sin extensiones, sin subprotocolos— cabe en un
fichero y se testea entero.

No negociamos permessage-deflate a propósito: Binance, Bybit y OKX funcionan
sin él, y evitarlo elimina toda la superficie de bugs de descompresión.
"""
from __future__ import annotations

import asyncio
import base64
import os
import ssl
import struct
from typing import Optional, Union
from urllib.parse import urlsplit

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA
_CONTROL = {OP_CLOSE, OP_PING, OP_PONG}

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_MAX_MESSAGE = 16 << 20  # 16 MiB


class WSError(Exception):
    """Cualquier fallo de protocolo o de transporte del socket."""


class WSClosed(WSError):
    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"websocket cerrado ({code}) {reason}".strip())
        self.code, self.reason = code, reason


class WSClient:
    def __init__(self, reader, writer, *, max_message_bytes: int):
        self._r = reader
        self._w = writer
        self._max = max_message_bytes
        self._closed = False
        self._send_lock = asyncio.Lock()
        # Contadores útiles para el health check; el recorder los publica tal cual.
        self.bytes_in = 0
        self.messages_in = 0

    # ---------------------------------------------------------------- connect
    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: Optional[dict] = None,
        open_timeout: float = 15.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> "WSClient":
        parts = urlsplit(url)
        secure = parts.scheme == "wss"
        if parts.scheme not in ("ws", "wss"):
            raise WSError(f"esquema no soportado: {parts.scheme!r}")
        port = parts.port or (443 if secure else 80)
        host = parts.hostname
        if not host:
            raise WSError(f"url sin host: {url!r}")

        ctx = None
        if secure:
            ctx = ssl_context or ssl.create_default_context()

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if secure else None),
            timeout=open_timeout,
        )

        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        req = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_hdr}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: liq-intel-recorder/0.1",
        ]
        for k, v in (headers or {}).items():
            req.append(f"{k}: {v}")
        writer.write(("\r\n".join(req) + "\r\n\r\n").encode())
        await writer.drain()

        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=open_timeout)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            writer.close()
            raise WSError(f"handshake truncado: {exc}") from exc

        lines = head.decode("latin-1").split("\r\n")
        status = lines[0]
        if " 101" not in status:
            writer.close()
            raise WSError(f"handshake rechazado: {status}")

        got = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                got[k.strip().lower()] = v.strip()
        import hashlib

        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if got.get("sec-websocket-accept") != expect:
            writer.close()
            raise WSError("Sec-WebSocket-Accept inválido")
        if got.get("upgrade", "").lower() != "websocket":
            writer.close()
            raise WSError("el servidor no hizo upgrade a websocket")
        if got.get("sec-websocket-extensions"):
            writer.close()
            raise WSError("el servidor negoció una extensión que no pedimos")

        return cls(reader, writer, max_message_bytes=max_message_bytes)

    # ------------------------------------------------------------------- recv
    async def recv(self, timeout: Optional[float] = None) -> Union[str, bytes]:
        """Devuelve el siguiente mensaje de datos.

        Los frames de control (ping/pong/close) se atienden por dentro: un ping
        recibido se responde con pong automáticamente. Un close del servidor
        levanta WSClosed, que es lo que el supervisor usa para reconectar.
        """
        if timeout is not None:
            return await asyncio.wait_for(self._recv(), timeout=timeout)
        return await self._recv()

    async def _recv(self) -> Union[str, bytes]:
        chunks: list[bytes] = []
        total = 0
        msg_op = None
        while True:
            fin, op, payload = await self._read_frame()

            if op in _CONTROL:
                if op == OP_PING:
                    await self._send_frame(OP_PONG, payload)
                elif op == OP_CLOSE:
                    code, reason = 1005, ""
                    if len(payload) >= 2:
                        code = struct.unpack("!H", payload[:2])[0]
                        reason = payload[2:].decode("utf-8", "replace")
                    await self._shutdown()
                    raise WSClosed(code, reason)
                # un pong entrante no requiere acción
                continue

            if msg_op is None:
                if op == OP_CONT:
                    raise WSError("frame de continuación sin mensaje iniciado")
                msg_op = op
            elif op != OP_CONT:
                raise WSError("mensaje nuevo antes de terminar el anterior")

            total += len(payload)
            if total > self._max:
                await self._shutdown()
                raise WSError(f"mensaje mayor que el límite ({self._max} bytes)")
            chunks.append(payload)

            if fin:
                data = b"".join(chunks)
                self.messages_in += 1
                self.bytes_in += total
                if msg_op == OP_TEXT:
                    return data.decode("utf-8")
                return data

    async def _read_frame(self):
        try:
            return await self._read_frame_inner()
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            # ConnectionError cubre Reset, Aborted y BrokenPipe. En Windows un
            # corte llega como ConnectionAbortedError (WinError 10053), que no
            # es ConnectionResetError: listarlas una a una deja huecos.
            # El peer cortó sin close handshake. Es la caída más común en
            # producción y no tiene nada de excepcional: se presenta como un
            # cierre 1006 para que el supervisor reconecte sin ruido.
            # _shutdown cierra el transporte: si solo marcáramos el flag, el
            # socket quedaría colgando hasta que lo recogiera el GC.
            await self._shutdown()
            raise WSClosed(1006, "conexión cortada sin close handshake") from exc

    async def _read_frame_inner(self):
        hdr = await self._r.readexactly(2)
        b0, b1 = hdr[0], hdr[1]
        fin = bool(b0 & 0x80)
        if b0 & 0x70:
            raise WSError("bits RSV activos sin extensión negociada")
        op = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._r.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._r.readexactly(8))[0]
        if op in _CONTROL:
            if not fin:
                raise WSError("frame de control fragmentado")
            if length > 125:
                raise WSError("frame de control demasiado largo")
        if length > self._max:
            raise WSError(f"frame mayor que el límite ({length} bytes)")
        mask = await self._r.readexactly(4) if masked else None
        payload = await self._r.readexactly(length) if length else b""
        if mask:
            payload = _apply_mask(payload, mask)
        return fin, op, payload

    # ------------------------------------------------------------------- send
    async def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            await self._send_frame(OP_TEXT, data.encode("utf-8"))
        else:
            await self._send_frame(OP_BINARY, data)

    async def ping(self, payload: bytes = b"") -> None:
        await self._send_frame(OP_PING, payload)

    async def _send_frame(self, op: int, payload: bytes) -> None:
        if self._closed:
            raise WSClosed(1006, "envío sobre socket cerrado")
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | op, 0x80 | n)
        elif n < (1 << 16):
            head = struct.pack("!BBH", 0x80 | op, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | op, 0x80 | 127, n)
        mask = os.urandom(4)
        async with self._send_lock:
            self._w.write(head + mask + _apply_mask(payload, mask))
            await self._w.drain()

    # ------------------------------------------------------------------ close
    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        try:
            body = struct.pack("!H", code) + reason.encode("utf-8")
            await asyncio.wait_for(self._send_frame(OP_CLOSE, body), timeout=3)
        except Exception:
            pass
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._w.close()
            await asyncio.wait_for(self._w.wait_closed(), timeout=3)
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


def _apply_mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    key = (mask * ((n >> 2) + 1))[:n]
    # XOR sobre enteros grandes: ~50x más rápido que un bucle byte a byte.
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
