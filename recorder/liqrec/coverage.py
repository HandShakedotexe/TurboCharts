"""Registro de cobertura: la diferencia entre "no pasó nada" y "no estábamos".

Sin esto, seis meses después miras un tramo vacío del histórico y no puedes
saber si el mercado estaba tranquilo o si el socket llevaba dos horas caído. Y
si no lo puedes saber, no puedes usar ese tramo para backtestear.

Escribe un JSONL por exchange/stream y día con tres tipos de evento:

    {"t":ms,"event":"up","detail":{...}}        conexión establecida y suscrita
    {"t":ms,"event":"down","detail":{...}}      conexión perdida, con el motivo
    {"t":ms,"event":"minute","messages":N,...}  latido por minuto con contadores

Los tramos "down" son los huecos. El latido por minuto convierte el silencio en
un dato positivo: 0 mensajes con el socket arriba es información, no ausencia.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def utc_ms() -> int:
    return int(time.time() * 1000)


class CoverageLog:
    def __init__(self, root: Path, exchange: str, stream: str):
        self.root = Path(root)
        self.exchange = exchange
        self.stream = stream
        self._lock = threading.Lock()
        self._fh = None
        self._day: Optional[str] = None

        self.state = "down"
        self.since = utc_ms()
        self.total_up_ms = 0
        self.total_down_ms = 0
        self.gaps = 0

    def _ensure_open(self, ts_ms: int) -> None:
        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day == self._day and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        d = self.root / "coverage" / self.exchange / self.stream
        d.mkdir(parents=True, exist_ok=True)
        self._fh = open(d / f"{day}.jsonl", "a", encoding="utf-8", buffering=1)
        self._day = day

    def _emit(self, event: str, **fields) -> None:
        ts = utc_ms()
        with self._lock:
            self._ensure_open(ts)
            rec = {"t": ts, "ex": self.exchange, "st": self.stream, "event": event}
            rec.update(fields)
            self._fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    # -------------------------------------------------------------- estados
    def mark_up(self, **detail) -> None:
        now = utc_ms()
        if self.state == "down":
            self.total_down_ms += now - self.since
            self.since = now
        self.state = "up"
        self._emit("up", **detail)

    def mark_down(self, reason: str, **detail) -> None:
        now = utc_ms()
        if self.state == "up":
            self.total_up_ms += now - self.since
            self.since = now
            self.gaps += 1
        self.state = "down"
        self._emit("down", reason=reason, **detail)

    def heartbeat(self, messages: int, bytes_in: int, **detail) -> None:
        self._emit("minute", messages=messages, bytes=bytes_in, state=self.state, **detail)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


# Cuanto puede tardar un evento en aparecer despues del anterior sin que
# eso, por si solo, signifique que el proceso estuvo muerto -- 3x
# `heartbeat_seconds` (60s por defecto, ver config.py) da margen de sobra
# para jitter normal del scheduler sin dejar pasar un corte real. Ver
# comentario de `scan_gaps` sobre por que hace falta esto ademas de los
# pares down/up.
IMPLICIT_GAP_THRESHOLD_MS = 180_000


def _read_events(path: Path) -> list[dict]:
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _gaps_from_events(events: list[dict]) -> list[dict]:
    """El nucleo real de `scan_gaps`/`scan_gaps_across_files` -- ver el
    docstring de `scan_gaps` para las dos fuentes de hueco que reconoce."""
    events = sorted(events, key=lambda e: e["t"])

    gaps, open_gap = [], None
    prev_t = None
    for e in events:
        if prev_t is not None and open_gap is None and (e["t"] - prev_t) > IMPLICIT_GAP_THRESHOLD_MS:
            gaps.append({"start": prev_t, "end": e["t"], "reason": "process_restart_implicit"})
        prev_t = e["t"]
        if e["event"] == "down" and open_gap is None:
            open_gap = {"start": e["t"], "reason": e.get("reason", "unknown")}
        elif e["event"] == "up" and open_gap is not None:
            open_gap["end"] = e["t"]
            gaps.append(open_gap)
            open_gap = None
    if open_gap is not None:
        open_gap["end"] = None  # sigue abierto
        gaps.append(open_gap)
    gaps.sort(key=lambda g: g["start"])
    return gaps


def scan_gaps(path: Path) -> list[dict]:
    """Reconstruye los tramos sin cobertura a partir de UN fichero de
    coverage (un exchange/stream/dia). Se usa desde el CLI
    (`python -m liqrec.doctor`) y desde los tests. Devuelve una lista de
    {"start": ms, "end": ms, "reason": str}.

    Dos fuentes de hueco, no una sola:

    1. Pares "down"/"up" emparejados -- el caso que el proceso SI alcanzo a
       detectar (desconexion de red, reconexion) mientras seguia vivo.
    2. Bug real encontrado el 2026-09-14 con un apagon de verdad: un corte
       de luz mata el proceso de un golpe, sin darle chance de escribir su
       propio evento "down" antes de morir -- al reconectar tras el reinicio
       solo aparece un "up" SUELTO, sin "down" que lo preceda. Con solo el
       emparejamiento de arriba, ese hueco (el caso mas importante de
       todos, justamente el que esta funcion existe para no dejar pasar)
       queda invisible: se confirmo con datos reales, `doctor` reportaba
       "sin huecos" para el dia de un apagon de ~110 minutos, verificado a
       mano contra las marcas de tiempo crudas. Cualquier salto entre DOS
       EVENTOS CONSECUTIVOS (sean del tipo que sean) mayor a
       `IMPLICIT_GAP_THRESHOLD_MS` significa que nada se escribio en el
       medio -- el proceso no pudo estar vivo y en silencio ese tiempo,
       ver el latido por minuto en el docstring del modulo. Se sintetiza
       ese tramo como hueco con motivo "process_restart_implicit".

    Ver `scan_gaps_across_files` para por que esta funcion, sobre UN SOLO
    fichero, no basta para `doctor.py`."""
    return _gaps_from_events(_read_events(path))


def scan_gaps_across_files(paths: list[Path]) -> list[dict]:
    """Igual que `scan_gaps`, pero tratando VARIOS ficheros (uno por dia,
    mismo exchange/stream) como una sola linea de tiempo continua.

    Bug real encontrado el 2026-09-17: el proceso entero
    murio durante ~30h, cruzando la medianoche del 16 al 17 -- `doctor.py`
    llamaba a `scan_gaps` UNA VEZ POR FICHERO (`for path in
    cov_root.rglob(...)`), y un hueco que cruza esa frontera queda
    invisible para las dos fuentes de arriba: el ultimo evento del fichero
    del dia 16 no tiene NADA despues suyo dentro de ESE fichero con que
    compararse (`IMPLICIT_GAP_THRESHOLD_MS` solo mira eventos consecutivos
    dentro de una misma llamada), y el primer evento del fichero del dia
    17 no tiene nada ANTES. `doctor` reporto "sin huecos" para los dos
    dias de un apagon real de mas de un dia completo -- verificado
    contra los ficheros crudos (`raw/<exchange>/liquidation/`), que si
    muestran el hueco con claridad (el ultimo `.jsonl` antes y el primer
    `.jsonl.gz` despues). Juntar los eventos de TODOS los ficheros del
    feed antes de buscar huecos, en vez de fichero por fichero, es la
    unica forma de que esto no se repita para el PROXIMO corte largo que
    cruce una medianoche."""
    events: list[dict] = []
    for p in paths:
        events.extend(_read_events(p))
    return _gaps_from_events(events)
