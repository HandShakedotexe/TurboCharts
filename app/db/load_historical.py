"""Carga en TimescaleDB los CSV que aterriza `ingest/binance_historical.py`.

Cada dataset tiene su propio "lag de publicacion" hacia `available_at` (ver
la cabecera de schema.sql para que es y por que importa). Binance no
documenta estos numeros para ningun endpoint historico, asi que son
estimaciones conservadoras, explicitas aqui para que se puedan revisar o
afinar sin arqueologia de codigo:

  - klines:  la vela cierra en `close_time` y esta disponible al instante
             (es el propio cierre). +1s de margen por streaming.
  - funding: se liquida y se difunde en el mismo momento de `calc_time`
             (aparece en el stream de markPrice al instante). +1s de margen.
  - metrics: NO documentado. Es un endpoint REST en bucket de 5 minutos; se
             asume conservador que el bucket no esta completo/disponible
             hasta que termina, es decir +5 minutos sobre `create_time`. Si
             en algun momento se mide el lag real, este es el sitio a
             cambiar -- una sola constante, no un valor repetido en el
             codigo.
  - aggTrades: un trade impreso es publico al instante (asi funciona el
             tape de cualquier exchange) -- sin lag, available_at = event_time.
             Solo se guardan los que superan `bigtrades.BIG_TRADE_THRESHOLD_USD`:
             mismo umbral que ya usa el heatmap en vivo (ver
             api/bigtrades.py), para que "big trade" signifique lo mismo
             seas cual seas la fuente.

Verificado contra ficheros reales (no supuesto): los dumps de `klines`
anteriores a 2022 NO llevan fila de cabecera; `fundingRate` y `metrics` sí la
llevan incluso en 2020-09, el primer mes publicado. `metrics` además trae
alguna fila duplicada de fabrica (mismo `create_time` dos veces seguidas) --
la PRIMARY KEY (exchange, symbol, event_time) de la tabla la absorbe sola.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from api.bigtrades import BIG_TRADE_THRESHOLD_USD

log = logging.getLogger("db.load_historical")

# Estado incremental: que ficheros ya se cargaron ENTEROS, para no volver a
# leerlos. Bug real de rendimiento (2026-09-17): sin esto,
# pedir solo "los aggTrades nuevos de BTCUSDT" (`ON CONFLICT DO NOTHING`
# hace que reejecutar sea INOFENSIVO, pero no GRATIS) releia los 9.1GB/109
# ficheros de los 3 meses YA cargados antes, ademas de los pocos dias
# nuevos que hacia falta agregar -- casi una hora para lo que deberian ser
# un par de minutos. `ON CONFLICT DO NOTHING` sigue siendo la red de
# seguridad de fondo (si el estado se pierde o se corrompe, recargar todo
# no duplica nada); esto es solo para no pagar ese costo cuando no hace
# falta. Vive en `data_root` (no junto al CSV de cada simbolo) porque
# `load_all` recorre klines/fundingRate/metrics/aggTrades de golpe.
LOAD_STATE_FILENAME = ".load_state.json"


def _load_state(data_root: Path) -> dict:
    path = data_root / LOAD_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Estado corrupto o ilegible -- recargar todo (correcto, solo
        # lento) es mejor que reventar el backfill entero por esto.
        return {}


def _save_state(data_root: Path, state: dict) -> None:
    (data_root / LOAD_STATE_FILENAME).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _fingerprint(path: Path) -> str:
    """Tamaño + mtime, no un hash del contenido -- los dumps de
    data.binance.vision son inmutables una vez publicados (nunca se
    reescriben in situ), asi que un cambio real en cualquiera de los dos
    ya implica "es un fichero distinto o se volvio a descargar", sin pagar
    el costo de leer el fichero entero solo para calcular un hash."""
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _unchanged_since_last_load(csv_path: Path, data_root: Path, state: dict) -> bool:
    key = str(csv_path.relative_to(data_root))
    return state.get(key) == _fingerprint(csv_path)


def _mark_loaded(csv_path: Path, data_root: Path, state: dict) -> None:
    state[str(csv_path.relative_to(data_root))] = _fingerprint(csv_path)

KLINE_PUBLISH_LAG = timedelta(seconds=1)
FUNDING_PUBLISH_LAG = timedelta(seconds=1)
METRICS_PUBLISH_LAG = timedelta(minutes=5)   # ver docstring: sin documentar por Binance
AGGTRADES_PUBLISH_LAG = timedelta(seconds=0)  # ver docstring: un trade es publico al instante

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]


def _ms_to_dt(ms: str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _naive_to_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _looks_like_header(first_cell: str) -> bool:
    return not first_cell.strip().lstrip("-").isdigit()


# ------------------------------------------------------------- parseo puro
def parse_klines_csv(path: Path, exchange: str, symbol: str, interval: str) -> Iterator[dict]:
    """Sin cabecera en los dumps anteriores a 2022: se detecta por fila, no
    por fecha del fichero, para no mantener una tabla de "que meses tienen
    cabecera"."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            if _looks_like_header(row[0]):
                continue
            rec = dict(zip(KLINE_COLUMNS, row))
            close_time = _ms_to_dt(rec["close_time"])
            yield {
                "exchange": exchange, "symbol": symbol, "interval": interval,
                "event_time": close_time,
                "available_at": close_time + KLINE_PUBLISH_LAG,
                "open": float(rec["open"]), "high": float(rec["high"]),
                "low": float(rec["low"]), "close": float(rec["close"]),
                "volume": float(rec["volume"]),
                "quote_volume": float(rec["quote_volume"]),
                "trades": int(rec["count"]),
                "taker_buy_volume": float(rec["taker_buy_volume"]),
                "taker_buy_quote_volume": float(rec["taker_buy_quote_volume"]),
                "source_file": path.name,
            }


def parse_funding_csv(path: Path, exchange: str, symbol: str) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            event_time = _ms_to_dt(row["calc_time"])
            yield {
                "exchange": exchange, "symbol": symbol,
                "event_time": event_time,
                "available_at": event_time + FUNDING_PUBLISH_LAG,
                "funding_interval_hours": int(row["funding_interval_hours"]) if row["funding_interval_hours"] else None,
                "funding_rate": float(row["last_funding_rate"]),
                "source_file": path.name,
            }


def parse_metrics_csv(path: Path, exchange: str) -> Iterator[dict]:
    """`symbol` viene en el propio CSV (columna `symbol`), no en la ruta."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            event_time = _naive_to_utc(row["create_time"])

            def _f(key):
                v = row.get(key, "")
                return float(v) if v not in (None, "") else None

            yield {
                "exchange": exchange, "symbol": row["symbol"],
                "event_time": event_time,
                "available_at": event_time + METRICS_PUBLISH_LAG,
                "sum_open_interest": _f("sum_open_interest"),
                "sum_open_interest_value": _f("sum_open_interest_value"),
                "count_toptrader_long_short_ratio": _f("count_toptrader_long_short_ratio"),
                "sum_toptrader_long_short_ratio": _f("sum_toptrader_long_short_ratio"),
                "count_long_short_ratio": _f("count_long_short_ratio"),
                "sum_taker_long_short_vol_ratio": _f("sum_taker_long_short_vol_ratio"),
                "source_file": path.name,
            }


AGGTRADES_COLUMNS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]


def parse_aggtrades_csv(path: Path, exchange: str, symbol: str,
                         threshold_usd: float = BIG_TRADE_THRESHOLD_USD) -> Iterator[dict]:
    """Igual que `parse_klines_csv`: cabecera detectada por fila (los dumps
    de 2019-2020 no la traen, los de 2025 sí -- verificado contra ficheros
    reales, no supuesto). Filtra a `price*quantity >= threshold_usd` --
    solo interesan los "big trades" que ya modela el heatmap en vivo
    (api/bigtrades.py), no CADA trade: guardar el tape completo de
    cualquier símbolo líquido son cientos de miles de filas por día."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            if _looks_like_header(row[0]):
                continue
            rec = dict(zip(AGGTRADES_COLUMNS, row))
            price = float(rec["price"])
            quantity = float(rec["quantity"])
            notional_usd = price * quantity
            if notional_usd < threshold_usd:
                continue
            event_time = _ms_to_dt(rec["transact_time"])
            yield {
                "exchange": exchange, "symbol": symbol,
                "agg_trade_id": int(rec["agg_trade_id"]),
                "event_time": event_time,
                "available_at": event_time + AGGTRADES_PUBLISH_LAG,
                "price": price,
                "notional_usd": notional_usd,
                # "true"/"false" en minuscula, verificado contra los CSV
                # reales (ver AGGTRADES_NO_HEADER/AGGTRADES_WITH_HEADER en
                # tests/test_load_historical.py) -- no "True"/"1".
                "is_buyer_maker": rec["is_buyer_maker"] == "true",
                "source_file": path.name,
            }


# --------------------------------------------------------- escritura en DB
CANDLES_COLS = ["exchange", "symbol", "interval", "event_time", "available_at", "open", "high",
                "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume",
                "taker_buy_quote_volume", "source_file"]
FUNDING_COLS = ["exchange", "symbol", "event_time", "available_at", "funding_interval_hours",
                "funding_rate", "source_file"]
METRICS_COLS = ["exchange", "symbol", "event_time", "available_at", "sum_open_interest",
                "sum_open_interest_value", "count_toptrader_long_short_ratio",
                "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                "sum_taker_long_short_vol_ratio", "source_file"]
AGGTRADES_COLS = ["exchange", "symbol", "agg_trade_id", "event_time", "available_at",
                  "price", "notional_usd", "is_buyer_maker", "source_file"]

def _upsert(conn, table: str, columns: list[str], rows: Iterable[dict], page_size: int = 5000) -> int:
    """INSERT ... ON CONFLICT DO NOTHING: reejecutar el loader sobre el mismo
    fichero no duplica filas (la PRIMARY KEY es exchange+simbolo[+interval]+event_time),
    y una fila duplicada de fabrica (ver metrics en el docstring del modulo)
    se descarta en vez de reventar la carga entera."""
    placeholders = ", ".join(f"%({c})s" for c in columns)
    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    total = 0
    batch = []
    with conn.cursor() as cur:
        for row in rows:
            batch.append(row)
            if len(batch) >= page_size:
                cur.executemany(sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            cur.executemany(sql, batch)
            total += len(batch)
    conn.commit()
    return total


# -------------------------------------------------------------- recorrido
def find_klines_files(root: Path, exchange: str = "binance") -> Iterator[tuple[Path, str, str]]:
    base = root / exchange / "klines"
    if not base.exists():
        return
    for symbol_dir in sorted(base.iterdir()):
        if not symbol_dir.is_dir():
            continue
        for interval_dir in sorted(symbol_dir.iterdir()):
            if not interval_dir.is_dir():
                continue
            for csv_path in sorted(interval_dir.glob("*.csv")):
                yield csv_path, symbol_dir.name, interval_dir.name


def find_symbol_files(root: Path, dataset: str, exchange: str = "binance") -> Iterator[tuple[Path, str]]:
    base = root / exchange / dataset
    if not base.exists():
        return
    for symbol_dir in sorted(base.iterdir()):
        if not symbol_dir.is_dir():
            continue
        for csv_path in sorted(symbol_dir.glob("*.csv")):
            yield csv_path, symbol_dir.name


def _table_counts(conn, tables: list[str]) -> dict:
    """Conteo real tras cargar: `load_all` solo sabe cuantas filas ENVIO al
    INSERT, no cuantas sobrevivieron el ON CONFLICT DO NOTHING -- metrics en
    particular trae mas duplicados de fabrica de lo que parece a simple
    vista. Reportar el numero enviado como si fuera el final es exactamente
    el tipo de dato que parece bueno y no lo es."""
    out = {}
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
    return out


ALL_DATASETS = ("klines", "fundingRate", "metrics", "aggTrades")


def load_all(data_root: Path, conn, exchange: str = "binance",
             datasets: tuple[str, ...] = ALL_DATASETS, force: bool = False) -> dict:
    """Devuelve filas ENVIADAS al INSERT por dataset (antes de deduplicar).
    Para el conteo real ya persistido, usa `_table_counts`.

    `datasets` filtra que subconjunto recorrer: `ON CONFLICT DO NOTHING`
    hace que reejecutar sobre datos ya cargados sea inofensivo, pero NO es
    gratis -- cada fila igual se manda a Postgres y se descarta ahi. Medido
    en pruebas reales (2026-08-30): recorrer klines+funding+metrics de los
    20 símbolos de Fase 1 solo para llegar a un puñado de CSV de aggTrades
    nuevos tarda mas de lo razonable. Por defecto sigue recorriendo los
    cuatro, para no cambiar el comportamiento de quien ya invoca esto sin
    el flag.

    Ademas, cada fichero que YA se cargo entero en una corrida anterior sin
    cambiar (mismo tamaño+mtime, ver `_fingerprint`) se salta sin leerlo --
    bug real de rendimiento (2026-09-17): sin esto, un backfill incremental
    de "unos pocos dias nuevos" de aggTrades releia los 9GB YA cargados de
    meses anteriores, casi una hora para lo que deberian ser un par de
    minutos. El estado se guarda en disco DESPUES DE CADA FICHERO (no solo
    al final) para que un corte a medio camino -- este mismo proyecto ya
    conoce de sobra ese riesgo -- no tire el progreso ya hecho:
    `ON CONFLICT DO NOTHING` sigue de red de seguridad si el estado
    se pierde o se corrompe. `force=True` (`--force-reload` en el CLI)
    ignora el estado y relee todo -- escotilla de escape para cuando cambia
    algo que el fingerprint no puede ver (p.ej. `BIG_TRADE_THRESHOLD_USD`)."""
    counts = {"candles": 0, "funding_rate": 0, "open_interest_metrics": 0, "big_trades": 0}
    state = {} if force else _load_state(data_root)

    def _skip(csv_path: Path) -> bool:
        return not force and _unchanged_since_last_load(csv_path, data_root, state)

    def _record(csv_path: Path) -> None:
        _mark_loaded(csv_path, data_root, state)
        _save_state(data_root, state)

    if "klines" in datasets:
        for csv_path, symbol, interval in find_klines_files(data_root, exchange):
            if _skip(csv_path):
                continue
            n = _upsert(conn, "candles", CANDLES_COLS,
                        parse_klines_csv(csv_path, exchange, symbol, interval))
            counts["candles"] += n
            _record(csv_path)
            log.info("candles  %-12s %-6s %-30s +%d filas", symbol, interval, csv_path.name, n)

    if "fundingRate" in datasets:
        for csv_path, symbol in find_symbol_files(data_root, "fundingRate", exchange):
            if _skip(csv_path):
                continue
            n = _upsert(conn, "funding_rate", FUNDING_COLS,
                        parse_funding_csv(csv_path, exchange, symbol))
            counts["funding_rate"] += n
            _record(csv_path)
            log.info("funding  %-12s %-30s +%d filas", symbol, csv_path.name, n)

    if "metrics" in datasets:
        for csv_path, symbol in find_symbol_files(data_root, "metrics", exchange):
            if _skip(csv_path):
                continue
            n = _upsert(conn, "open_interest_metrics", METRICS_COLS,
                        parse_metrics_csv(csv_path, exchange))
            counts["open_interest_metrics"] += n
            _record(csv_path)
            log.info("metrics  %-12s %-30s +%d filas", symbol, csv_path.name, n)

    if "aggTrades" in datasets:
        for csv_path, symbol in find_symbol_files(data_root, "aggTrades", exchange):
            if _skip(csv_path):
                continue
            n = _upsert(conn, "big_trades", AGGTRADES_COLS,
                        parse_aggtrades_csv(csv_path, exchange, symbol))
            counts["big_trades"] += n
            _record(csv_path)
            log.info("aggTrades %-12s %-30s +%d filas", symbol, csv_path.name, n)

    return counts


def _connect():
    import psycopg  # import perezoso: solo hace falta si se usa la DB de verdad
    dsn = os.environ.get("LIQINTEL_DSN") or (
        f"host=127.0.0.1 port=5432 "
        f"dbname={os.environ.get('POSTGRES_DB', 'liqintel')} "
        f"user={os.environ.get('POSTGRES_USER', 'liqintel')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')}"
    )
    return psycopg.connect(dsn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="db.load_historical")
    ap.add_argument("--data-dir", default="../../data/historical")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--datasets", default=",".join(ALL_DATASETS),
                     help="subconjunto separado por comas de: " + ",".join(ALL_DATASETS))
    ap.add_argument("--force-reload", action="store_true",
                     help="ignora el estado incremental (.load_state.json) y relee TODOS los "
                          "ficheros, aunque no hayan cambiado -- usar solo si cambio algo que el "
                          "fingerprint tamaño+mtime no puede ver, p.ej. BIG_TRADE_THRESHOLD_USD")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    datasets = tuple(d.strip() for d in a.datasets.split(",") if d.strip())
    conn = _connect()
    try:
        sent = load_all(Path(a.data_dir), conn, a.exchange, datasets, force=a.force_reload)
        real = _table_counts(conn, list(sent.keys()))
    finally:
        conn.close()
    log.info("filas enviadas (antes de deduplicar): %s", sent)
    log.info("filas en la tabla ahora mismo:        %s", real)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
