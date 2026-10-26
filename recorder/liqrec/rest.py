"""GET HTTP mínimo y asíncrono, sobre urllib en un hilo.

Los pollers hacen del orden de un par de peticiones por segundo como mucho, así
que no merece la pena una dependencia de cliente HTTP asíncrono. urllib está en
la stdlib, entiende proxies del entorno y no se rompe entre versiones.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import urllib.error
import urllib.request
from typing import Any, Optional

USER_AGENT = "liq-intel-recorder/0.1"


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} en {url}: {body[:200]}")
        self.status, self.body, self.url = status, body, url


def _get_sync(url: str, timeout: float, headers: Optional[dict]) -> Any:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise HttpError(exc.code, detail, url) from exc


async def get_json(url: str, *, timeout: float = 15.0, headers: Optional[dict] = None) -> Any:
    return await asyncio.to_thread(_get_sync, url, timeout, headers)
