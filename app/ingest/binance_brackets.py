"""Descarga los tramos de notional y maintenance margin rate (MMR) de
Binance USD-M -- el dato que hace falta para el modelo de heatmap
(P_liq = Pe * (1 - 1/L + mmr)) y que, sin esto, habria que inventar.

`GET /fapi/v1/leverageBracket` (el endpoint "oficial" de la API de trading)
exige API key firmada aunque los tramos son iguales para todo el mundo. La
propia pagina publica de Binance (binance.com/en/futures/trading-rules/...)
los pinta sin que el visitante inicie sesion, llamando a un endpoint interno
que SI es publico:

    POST https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets
    body: {}

Verificado con curl plano (sin navegador, sin cookies) el 2026-08-28: 200 OK,
1017 simbolos, MMR de nivel 1 de BTCUSDT = 0.004 (0.4%), que coincide con el
valor ampliamente conocido para ese par. No es un endpoint documentado por
Binance como API publica formal -- es el que usa su propia web -- asi que
puede cambiar de forma sin aviso; si un dia devuelve otra forma, este es el
unico sitio que hay que tocar.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Iterator

from .binance_historical import default_fetch

log = logging.getLogger("ingest.binance_brackets")

BRACKETS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets"

BRACKETS_COLS = ["exchange", "symbol", "fetched_at", "bracket_seq", "notional_floor",
                  "notional_cap", "maintenance_margin_rate", "cum_maintenance_amount",
                  "min_leverage", "max_leverage"]


def fetch_brackets(fetch: Callable[[str], bytes] = default_fetch) -> dict:
    """`default_fetch` es GET; este endpoint es POST con cuerpo `{}` -- se
    hace la peticion aqui mismo en vez de forzarla dentro de un fetch generico."""
    import urllib.request

    req = urllib.request.Request(
        BRACKETS_URL, data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "liq-intel-ingest/0.1"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") != "000000":
        raise RuntimeError(f"respuesta inesperada de brackets: {body.get('code')} {body.get('message')}")
    return body["data"]["brackets"]


def parse_brackets(raw: list[dict], exchange: str = "binance",
                    fetched_at: datetime | None = None) -> Iterator[dict]:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    for entry in raw:
        for b in entry["riskBrackets"]:
            yield {
                "exchange": exchange, "symbol": entry["symbol"], "fetched_at": fetched_at,
                "bracket_seq": b["bracketSeq"],
                "notional_floor": b["bracketNotionalFloor"],
                "notional_cap": b["bracketNotionalCap"],
                "maintenance_margin_rate": b["bracketMaintenanceMarginRate"],
                "cum_maintenance_amount": b["cumFastMaintenanceAmount"],
                "min_leverage": b.get("minOpenPosLeverage"),
                "max_leverage": b["maxOpenPosLeverage"],
            }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ingest.binance_brackets")
    ap.add_argument("--symbols", default="",
                     help="lista separada por comas; vacio = los ~1000 que devuelve Binance")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    from db.load_historical import _connect, _upsert  # import perezoso: solo hace falta si se usa la DB de verdad

    raw = fetch_brackets()
    wanted = {s.strip() for s in a.symbols.split(",") if s.strip()}
    if wanted:
        raw = [r for r in raw if r["symbol"] in wanted]

    fetched_at = datetime.now(timezone.utc)
    rows = list(parse_brackets(raw, fetched_at=fetched_at))
    log.info("%d simbolos, %d tramos, fetched_at=%s", len(raw), len(rows), fetched_at.isoformat())

    conn = _connect()
    try:
        n = _upsert(conn, "margin_brackets", BRACKETS_COLS, rows)
    finally:
        conn.close()
    log.info("insertados (enviados): %d", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
