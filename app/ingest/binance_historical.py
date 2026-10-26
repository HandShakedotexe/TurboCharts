"""Descarga los volcados historicos publicos de Binance USD-M.

data.binance.vision publica, desde 2020-09-01, velas (klines), funding rate
y `metrics` (open interest y ratios long/short) para cada símbolo. Las
liquidaciones de perpetuos USD-M NO tienen histórico público en ningún
sitio -- por eso existe el grabador de Fase 0 -- pero esto sí existe y se
puede descargar cuando haga falta, así que Fase 1
no tiene que esperar al grabador para arrancar.

Cadencia de publicación, verificada contra el propio bucket (no es la misma
para los tres):
  - klines:      mensual, un zip por símbolo/intervalo/mes
  - fundingRate: mensual, un zip por símbolo/mes
  - metrics:     DIARIO, un zip por símbolo/día (no existe versión mensual)
  - aggTrades:   diario Y mensual (ambas existen desde 2019) -- aquí se usa
                 diario, igual que metrics: un backfill corto (p.ej. una
                 semana) no necesita bajar zips mensuales de cientos de
                 MB por un puñado de días

Cada zip trae un .CHECKSUM (SHA256) al lado. Se verifica siempre antes de
aceptar un fichero como bueno: una descarga cortada da un zip que parece
válido pero no lo es, y aceptar eso en silencio corrompe todo lo que se
calcule después sobre esos datos -- exactamente la clase de fallo silencioso
que este proyecto existe para evitar (invariante 1).

Los CSV se guardan CRUDOS tal como los publica Binance, sin parsear ni
normalizar, por la misma razón que el grabador guarda el mensaje crudo: si
Binance cambia el formato de aquí a un año, se rehace el parseo sobre lo ya
descargado sin volver a bajar nada.

Cada intento (ok, no disponible, checksum roto, error) queda en un manifest
JSONL junto a los datos. Un mes sin fichero por sí solo no dice si no se
intentó, si no existía todavía, o si falló -- el manifest sí lo dice,
igual que `coverage/` en el grabador distingue silencio de mercado tranquilo.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger("ingest.binance_historical")

BASE_URL = "https://data.binance.vision/data/futures/um"
USER_AGENT = "liq-intel-ingest/0.1"


class NotAvailable(Exception):
    """El periodo pedido no existe en data.binance.vision (404 real).

    Normal para fechas anteriores a que Binance empezara a publicar ese
    dataset, o para el mes/día en curso, que todavía no se ha cerrado.
    """


class ChecksumMismatch(Exception):
    """El SHA256 del zip descargado no coincide con el .CHECKSUM publicado.

    No se acepta el fichero: mejor un hueco visible en el manifest que un
    dato corrupto que parece bueno.
    """


def default_fetch(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotAvailable(url) from None
        raise
    except urllib.error.URLError as exc:
        raise RuntimeError(f"no se pudo conectar a {url}: {exc.reason}") from exc


# --------------------------------------------------------------- periodos
def months_between(start: date, end: date) -> Iterator[date]:
    """Un date por cada mes, con day=1, de start a end inclusive (por mes)."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 1)
        m += 1
        if m > 12:
            m = 1
            y += 1


def days_between(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d = date.fromordinal(d.toordinal() + 1)


# --------------------------------------------------------- rutas por dataset
def kline_target(symbol: str, interval: str, period: date) -> tuple[str, str]:
    fname = f"{symbol}-{interval}-{period.year:04d}-{period.month:02d}.zip"
    return f"{BASE_URL}/monthly/klines/{symbol}/{interval}/{fname}", fname


def funding_target(symbol: str, period: date) -> tuple[str, str]:
    fname = f"{symbol}-fundingRate-{period.year:04d}-{period.month:02d}.zip"
    return f"{BASE_URL}/monthly/fundingRate/{symbol}/{fname}", fname


def metrics_target(symbol: str, period: date) -> tuple[str, str]:
    fname = f"{symbol}-metrics-{period.isoformat()}.zip"
    return f"{BASE_URL}/daily/metrics/{symbol}/{fname}", fname


def aggtrades_target(symbol: str, period: date) -> tuple[str, str]:
    fname = f"{symbol}-aggTrades-{period.isoformat()}.zip"
    return f"{BASE_URL}/daily/aggTrades/{symbol}/{fname}", fname


# ------------------------------------------------------------- descarga
def _checksum_ok(data: bytes, checksum_text: str) -> bool:
    expected = checksum_text.strip().split()[0].lower() if checksum_text.strip() else ""
    return bool(expected) and hashlib.sha256(data).hexdigest() == expected


def download_verified(url: str, fetch: Callable[[str], bytes] = default_fetch) -> bytes:
    """Descarga `url` y comprueba su .CHECKSUM antes de devolverlo."""
    body = fetch(url)
    try:
        checksum_text = fetch(url + ".CHECKSUM").decode("ascii", "replace")
    except NotAvailable:
        raise RuntimeError(f"{url}: no hay .CHECKSUM publicado, no se puede verificar") from None
    if not _checksum_ok(body, checksum_text):
        raise ChecksumMismatch(f"{url}: SHA256 no coincide con el .CHECKSUM publicado")
    return body


def extract_single_csv(zip_bytes: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise RuntimeError(f"zip con {len(names)} ficheros, se esperaba exactamente 1: {names}")
        return names[0], z.read(names[0])


# --------------------------------------------------------------- manifest
def _manifest_path(dest_dir: Path) -> Path:
    return dest_dir / "manifest.jsonl"


def _record(dest_dir: Path, **fields) -> None:
    fields.setdefault("ingested_at", datetime.now(timezone.utc).isoformat())
    dest_dir.mkdir(parents=True, exist_ok=True)
    with open(_manifest_path(dest_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields, ensure_ascii=False) + "\n")


def _already_landed(dest_dir: Path, csv_name: str) -> bool:
    return (dest_dir / csv_name).exists()


# ---------------------------------------------------------------- sync
@dataclass
class SyncResult:
    ok: int = 0
    not_available: int = 0
    failed: int = 0


def _sync_one(dest_dir: Path, csv_name: str, url: str, fetch: Callable[[str], bytes]) -> str:
    """Descarga, verifica y aterriza un fichero. Devuelve el status final."""
    if _already_landed(dest_dir, csv_name):
        return "skipped_existing"
    try:
        zip_bytes = download_verified(url, fetch=fetch)
        inner_name, csv_bytes = extract_single_csv(zip_bytes)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / csv_name).write_bytes(csv_bytes)
        _record(dest_dir, file=csv_name, source=url, status="ok", bytes=len(csv_bytes))
        return "ok"
    except NotAvailable:
        _record(dest_dir, file=csv_name, source=url, status="not_available")
        return "not_available"
    except ChecksumMismatch as exc:
        _record(dest_dir, file=csv_name, source=url, status="checksum_mismatch", detail=str(exc))
        return "failed"
    except Exception as exc:
        _record(dest_dir, file=csv_name, source=url, status="error",
                 detail=f"{type(exc).__name__}: {exc}"[:300])
        return "failed"


def sync_klines(symbol: str, interval: str, start: date, end: date, data_root: Path,
                 fetch: Callable[[str], bytes] = default_fetch, pace_seconds: float = 0.2) -> SyncResult:
    dest = data_root / "binance" / "klines" / symbol / interval
    result = SyncResult()
    for period in months_between(start, end):
        url, fname = kline_target(symbol, interval, period)
        csv_name = fname[:-4] + ".csv"
        status = _sync_one(dest, csv_name, url, fetch)
        _tally(result, status)
        if status not in ("skipped_existing",):
            time.sleep(pace_seconds)
    return result


def sync_funding(symbol: str, start: date, end: date, data_root: Path,
                  fetch: Callable[[str], bytes] = default_fetch, pace_seconds: float = 0.2) -> SyncResult:
    dest = data_root / "binance" / "fundingRate" / symbol
    result = SyncResult()
    for period in months_between(start, end):
        url, fname = funding_target(symbol, period)
        csv_name = fname[:-4] + ".csv"
        status = _sync_one(dest, csv_name, url, fetch)
        _tally(result, status)
        if status not in ("skipped_existing",):
            time.sleep(pace_seconds)
    return result


def sync_metrics(symbol: str, start: date, end: date, data_root: Path,
                  fetch: Callable[[str], bytes] = default_fetch, pace_seconds: float = 0.2) -> SyncResult:
    dest = data_root / "binance" / "metrics" / symbol
    result = SyncResult()
    for period in days_between(start, end):
        url, fname = metrics_target(symbol, period)
        csv_name = fname[:-4] + ".csv"
        status = _sync_one(dest, csv_name, url, fetch)
        _tally(result, status)
        if status not in ("skipped_existing",):
            time.sleep(pace_seconds)
    return result


def sync_aggtrades(symbol: str, start: date, end: date, data_root: Path,
                    fetch: Callable[[str], bytes] = default_fetch, pace_seconds: float = 0.2) -> SyncResult:
    dest = data_root / "binance" / "aggTrades" / symbol
    result = SyncResult()
    for period in days_between(start, end):
        url, fname = aggtrades_target(symbol, period)
        csv_name = fname[:-4] + ".csv"
        status = _sync_one(dest, csv_name, url, fetch)
        _tally(result, status)
        if status not in ("skipped_existing",):
            time.sleep(pace_seconds)
    return result


def _tally(result: SyncResult, status: str) -> None:
    if status in ("ok", "skipped_existing"):
        result.ok += 1
    elif status == "not_available":
        result.not_available += 1
    else:
        result.failed += 1


# ----------------------------------------------------------------- CLI
def _parse_yyyy_mm(s: str) -> date:
    y, m = s.split("-")
    return date(int(y), int(m), 1)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ingest.binance_historical")
    ap.add_argument("--symbol", required=True, help="p.ej. BTCUSDT")
    ap.add_argument("--start", help="YYYY-MM, primer mes a incluir")
    ap.add_argument("--end", help="YYYY-MM, ultimo mes a incluir")
    ap.add_argument("--days", type=int,
                     help="alternativa a --start/--end para datasets diarios (aggTrades, "
                          "metrics): los ultimos N dias completos (sin contar hoy, que "
                          "todavia no tiene volcado publicado) -- para un backfill corto "
                          "tipo 'la ultima semana' sin tener que pensar en meses")
    ap.add_argument("--data-dir", default="../../data/historical",
                     help="raiz donde aterrizan los CSV (por defecto ../../data/historical, "
                          "junto a data/raw/ del grabador -- nunca dentro del repo)")
    ap.add_argument("--klines-interval", default="1h")
    ap.add_argument("--datasets", default="klines,fundingRate,metrics",
                     help="subconjunto separado por comas de: klines,fundingRate,metrics,aggTrades")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if a.days is not None:
        if a.start or a.end:
            log.error("--days no se combina con --start/--end")
            return 2
        end_day = date.today() - timedelta(days=1)
        start_day = end_day - timedelta(days=a.days - 1)
        start = end_month = start_day  # solo relevante para klines/fundingRate, que no usan --days
    else:
        if not a.start or not a.end:
            log.error("hace falta --start y --end (YYYY-MM), o --days para un rango diario")
            return 2
        start = _parse_yyyy_mm(a.start)
        end_month = _parse_yyyy_mm(a.end)
        end_day = date(end_month.year, end_month.month, monthrange(end_month.year, end_month.month)[1])
        start_day = start
    data_root = Path(a.data_dir)
    datasets = {d.strip() for d in a.datasets.split(",") if d.strip()}

    for name in datasets:
        if name == "klines":
            r = sync_klines(a.symbol, a.klines_interval, start, end_month, data_root)
        elif name == "fundingRate":
            r = sync_funding(a.symbol, start, end_month, data_root)
        elif name == "metrics":
            r = sync_metrics(a.symbol, start_day, end_day, data_root)
        elif name == "aggTrades":
            r = sync_aggtrades(a.symbol, start_day, end_day, data_root)
        else:
            log.error("dataset desconocido: %s (usa klines,fundingRate,metrics,aggTrades)", name)
            return 2
        log.info("[%s/%s] ok=%d no_disponible=%d fallos=%d",
                  a.symbol, name, r.ok, r.not_available, r.failed)
        if r.failed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
