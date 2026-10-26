"""Escritura append-only de eventos crudos.

Decisiones deliberadas:

1. Se escribe el mensaje TAL CUAL llega, antes de normalizar nada. La
   normalización se puede rehacer sobre el fichero crudo cuando descubramos que
   nos equivocamos; el mensaje perdido no vuelve.

2. Los ficheros de la hora en curso se escriben SIN comprimir y se comprimen al
   rotar. Un gzip cortado a media escritura por un corte de luz es basura; un
   JSONL cortado pierde como mucho la última línea.

3. fsync periódico (no por línea). Con fsync por línea el disco se convierte en
   el cuello de botella en una cascada, que es justo cuando no puedes permitirte
   ir lento.

Layout en disco:

    {root}/raw/{exchange}/{stream}/{YYYY-MM-DD}/{HH}.jsonl[.gz]
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def utc_ms() -> int:
    return int(time.time() * 1000)


class RawWriter:
    def __init__(
        self,
        root: Path,
        exchange: str,
        stream: str,
        *,
        fsync_seconds: float = 5.0,
        compress_on_rotate: bool = True,
    ):
        self.root = Path(root)
        self.exchange = exchange
        self.stream = stream
        self.fsync_seconds = fsync_seconds
        self.compress_on_rotate = compress_on_rotate

        self._fh = None
        self._current_hour: Optional[str] = None
        self._current_path: Optional[Path] = None
        self._last_sync = 0.0
        self._lock = threading.Lock()

        self.lines_written = 0
        self.bytes_written = 0

    # ------------------------------------------------------------------ paths
    def _paths_for(self, ts_ms: int):
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        hour = dt.strftime("%H")
        d = self.root / "raw" / self.exchange / self.stream / day
        return f"{day}T{hour}", d, d / f"{hour}.jsonl"

    # ------------------------------------------------------------------ write
    def write(self, payload, *, received_at: Optional[int] = None, extra: Optional[dict] = None) -> None:
        rx = received_at if received_at is not None else utc_ms()
        rec = {"rx": rx, "ex": self.exchange, "st": self.stream, "raw": payload}
        if extra:
            rec.update(extra)
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._ensure_open(rx)
            n = self._fh.write(line + "\n")
            self.lines_written += 1
            self.bytes_written += n
            now = time.monotonic()
            if now - self._last_sync >= self.fsync_seconds:
                self._flush_locked()
                self._last_sync = now

    def _ensure_open(self, ts_ms: int) -> None:
        key, directory, path = self._paths_for(ts_ms)
        if key == self._current_hour and self._fh is not None:
            return
        self._close_locked()
        directory.mkdir(parents=True, exist_ok=True)
        # 'a' y no 'w': si el proceso reinicia dentro de la misma hora, se
        # continúa el fichero en lugar de machacarlo.
        self._fh = open(path, "a", encoding="utf-8", buffering=1 << 16)
        self._current_hour = key
        self._current_path = path
        self._last_sync = time.monotonic()

    # ----------------------------------------------------------------- rotate
    def _flush_locked(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def _close_locked(self) -> None:
        if self._fh is None:
            return
        prev = self._current_path
        try:
            self._flush_locked()
        finally:
            self._fh.close()
            self._fh = None
            self._current_hour = None
            self._current_path = None
        if self.compress_on_rotate and prev is not None and prev.exists():
            _compress(prev)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _compress(path: Path) -> None:
    """Comprime un fichero cerrado a .gz de forma atómica y borra el original."""
    gz_tmp = path.with_suffix(path.suffix + ".gz.tmp")
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        # Ya existía (reinicio dentro de la misma hora): se concatena el crudo
        # en un fichero aparte para no perderlo ni corromper el .gz.
        alt = path.with_name(path.stem + f".{int(time.time())}{path.suffix}")
        path.rename(alt)
        path = alt
        gz_tmp = path.with_suffix(path.suffix + ".gz.tmp")
        gz = path.with_suffix(path.suffix + ".gz")
    try:
        with open(path, "rb") as src, gzip.open(gz_tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        os.replace(gz_tmp, gz)
        path.unlink()
    except Exception:
        # Comprimir es una optimización: si falla, el crudo se queda y se avisa.
        if gz_tmp.exists():
            try:
                gz_tmp.unlink()
            except OSError:
                pass
        raise
