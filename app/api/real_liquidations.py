"""Alertas de liquidacion REALES -- lee directo de los ficheros crudos del
grabador (Fase 0), NO de la DB: no hay tabla todavia para esto (invariante 5,
"reales y proyectadas nunca en la misma tabla" -- esta ni siquiera tiene
tabla, ver "Fuera de alcance" del plan de esta fase).

Mismo patron de cache que api/bigtrades.py (`_Buffer` + candado por
simbolo + TTL), pero la fuente es disco LOCAL, no un REST externo -- TTL
mucho mas corto porque no hay un limite de tasa ajeno que cuidar.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# recorder/ vive fuera de app/ (paquete hermano, deploy independiente) --
# se añade al sys.path para reusar liqrec.normalize.from_binance TAL CUAL,
# en vez de reimplementar el mapeo de lados, marcado como "lo
# mas facil de equivocar de todo el proyecto". No añade NINGUNA
# dependencia externa a recorder/ (liqrec.normalize ya es solo stdlib) --
# solo permite que app/ (que ya depende de FastAPI y demas) lo importe.
_RECORDER_ROOT = Path(__file__).resolve().parents[2] / "recorder"
if str(_RECORDER_ROOT) not in sys.path:
    sys.path.insert(0, str(_RECORDER_ROOT))

from liqrec.normalize import Liquidation, NormalizationError, from_binance  # noqa: E402

CACHE_TTL_SECONDS = 5.0


def _data_dir() -> Path:
    return Path(os.environ.get("LIQREC_DATA_DIR", "./data"))


def _hour_path_candidates(data_dir: Path, exchange: str, stream: str, ts: datetime) -> list[Path]:
    base = data_dir / "raw" / exchange / stream / ts.strftime("%Y-%m-%d") / ts.strftime("%H")
    return [base.with_suffix(".jsonl"), Path(str(base) + ".jsonl.gz")]


def _hour_files(data_dir: Path, exchange: str, stream: str, now: datetime) -> list[Path]:
    """La hora en curso (sin comprimir, ver recorder/liqrec/writer.py) y
    la anterior (puede ya estar comprimida si acaba de rotar) -- suficiente
    para no perder eventos justo despues de una rotacion de hora."""
    paths = []
    for ts in (now, now - timedelta(hours=1)):
        for candidate in _hour_path_candidates(data_dir, exchange, stream, ts):
            if candidate.exists():
                paths.append(candidate)
    return paths


def _hour_files_since(data_dir: Path, exchange: str, stream: str, since: datetime, now: datetime) -> list[Path]:
    """Igual que `_hour_files` pero para TODAS las horas entre `since` y
    `now`, no solo la actual y la anterior -- lo que hace falta para que un
    marcador de liquidacion siga visible al desplazarse mas de 1-2h hacia
    atras en el chart (reporte del usuario, 2026-09-04: "no persisten a lo
    largo del grafico"). El grabador solo lleva corriendo desde 2026-08-27,
    asi que en la practica esto nunca recorre mas de un puñado de dias --
    cientos de ficheros pequeños como mucho, no miles."""
    paths = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    end = now.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        for candidate in _hour_path_candidates(data_dir, exchange, stream, cursor):
            if candidate.exists():
                paths.append(candidate)
        cursor += timedelta(hours=1)
    return paths


def _read_lines(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from f
    else:
        with open(path, encoding="utf-8") as f:
            yield from f


def fetch_recent_liquidations(symbol: str, limit: int = 50, data_dir: Optional[Path] = None) -> list[Liquidation]:
    """Lee las 1-2 horas mas recientes del stream REAL de liquidaciones de
    Binance grabado en disco, normaliza cada linea con
    `liqrec.normalize.from_binance` y filtra por simbolo. Salta en
    silencio lineas que no normalizan -- un mensaje que no es forceOrder,
    o una linea cortada a media escritura en la hora en curso (ver
    recorder/liqrec/writer.py sobre por que eso puede pasar y por que es
    preferible a perder el fichero entero)."""
    root = data_dir or _data_dir()
    now = datetime.now(timezone.utc)
    events: list[Liquidation] = []
    for path in _hour_files(root, "binance", "liquidation", now):
        for line in _read_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                liq = from_binance(rec["raw"], ingested_at=rec.get("rx", 0))
            except (NormalizationError, KeyError, ValueError, TypeError):
                continue
            if liq.symbol == symbol:
                events.append(liq)
    events.sort(key=lambda e: e.event_time, reverse=True)
    return events[:limit]


def fetch_liquidations_since(
    symbol: str, since: datetime, limit: int = 5000, data_dir: Optional[Path] = None,
) -> list[Liquidation]:
    """Igual que `fetch_recent_liquidations` pero recorre TODAS las horas
    desde `since` hasta ahora (ver `_hour_files_since`) en vez de solo la
    hora en curso y la anterior -- para que los marcadores de liquidacion
    real puedan cubrir todo el rango visible del chart, no solo los ultimos
    minutos. Mismo criterio de normalizacion/filtro por simbolo que la
    version "recent"."""
    root = data_dir or _data_dir()
    now = datetime.now(timezone.utc)
    events: list[Liquidation] = []
    for path in _hour_files_since(root, "binance", "liquidation", since, now):
        for line in _read_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                liq = from_binance(rec["raw"], ingested_at=rec.get("rx", 0))
            except (NormalizationError, KeyError, ValueError, TypeError):
                continue
            if liq.symbol == symbol and liq.event_time >= int(since.timestamp() * 1000):
                events.append(liq)
    events.sort(key=lambda e: e.event_time, reverse=True)
    return events[:limit]


@dataclass
class _Buffer:
    events: list[Liquidation] = field(default_factory=list)
    fetched_at: float = 0.0


_buffers: dict[str, _Buffer] = {}
_range_buffers: dict[tuple[str, int], _Buffer] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# TTL mas largo que CACHE_TTL_SECONDS (5s, pensado para la cola en vivo):
# un barrido de rango relee potencialmente cientos de ficheros de disco,
# mucho mas caro que las 1-2 horas de la cola -- 20s es un compromiso entre
# "no releer disco en cada poll de 8s del frontend" (ver LiquidationMarkers.tsx)
# y no quedarse demasiado desfasado de eventos nuevos en la hora en curso.
RANGE_CACHE_TTL_SECONDS = 20.0


def _lock_for(symbol: str) -> threading.Lock:
    with _locks_guard:
        if symbol not in _locks:
            _locks[symbol] = threading.Lock()
        return _locks[symbol]


def get_recent_liquidations_cached(symbol: str, limit: int = 50) -> list[Liquidation]:
    symbol = symbol.upper()
    now = time.monotonic()
    buf = _buffers.get(symbol)
    if buf and now - buf.fetched_at < CACHE_TTL_SECONDS:
        return buf.events[:limit]
    with _lock_for(symbol):
        now = time.monotonic()
        buf = _buffers.get(symbol)
        if buf and now - buf.fetched_at < CACHE_TTL_SECONDS:
            return buf.events[:limit]
        # Se guarda mas de lo pedido (200) para que un `limit` mas grande
        # en una llamada posterior, dentro del mismo TTL, no obligue a
        # releer disco -- barato, son enteros ya parseados en memoria.
        events = fetch_recent_liquidations(symbol, limit=max(limit, 200))
        _buffers[symbol] = _Buffer(events=events, fetched_at=time.monotonic())
        return events[:limit]


def get_liquidations_since_cached(symbol: str, since: datetime, limit: int = 5000) -> list[Liquidation]:
    """Cache separado de `get_recent_liquidations_cached` (clave incluye la
    hora de `since`, redondeada -- distintas ventanas de lookback no deben
    compartir ni invalidar el cache de la otra)."""
    symbol = symbol.upper()
    since_key = int(since.timestamp() // 3600)
    key = (symbol, since_key)
    now = time.monotonic()
    buf = _range_buffers.get(key)
    if buf and now - buf.fetched_at < RANGE_CACHE_TTL_SECONDS:
        return buf.events[:limit]
    with _lock_for(f"{symbol}:{since_key}"):
        now = time.monotonic()
        buf = _range_buffers.get(key)
        if buf and now - buf.fetched_at < RANGE_CACHE_TTL_SECONDS:
            return buf.events[:limit]
        events = fetch_liquidations_since(symbol, since, limit=limit)
        _range_buffers[key] = _Buffer(events=events, fetched_at=time.monotonic())
        return events[:limit]
