"""`python -m liqrec.doctor` — informe de qué se ha grabado y qué falta.

Contesta las dos preguntas que se hacen dentro de seis meses:
¿cuántos eventos tengo por exchange y día, y en qué tramos no estaba escuchando?
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .coverage import scan_gaps_across_files


def _open(p: Path):
    return gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz" else open(p, "r", encoding="utf-8")


def count_raw(root: Path) -> dict:
    counts: dict = defaultdict(lambda: defaultdict(int))
    for path in sorted((root / "raw").rglob("*.jsonl*")):
        if path.name.endswith(".tmp"):
            continue
        parts = path.relative_to(root / "raw").parts
        if len(parts) < 4:
            continue
        exchange, stream, day = parts[0], parts[1], parts[2]
        n = 0
        try:
            with _open(path) as fh:
                for _ in fh:
                    n += 1
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            print(f"  ! ilegible: {path} ({exc})")
            continue
        counts[f"{exchange}/{stream}"][day] += n
    return counts


def feeds_with_coverage(root: Path) -> set:
    """Feeds que han estado conectados alguna vez, según el registro de cobertura.

    Es la lista contra la que hay que comparar los datos: un feed que aparece
    aquí y no en los datos NO es un feed que no existe, es un feed que estuvo
    conectado y no grabó nada.
    """
    out = set()
    cov = root / "coverage"
    if not cov.exists():
        return out
    for path in cov.rglob("*.jsonl"):
        parts = path.relative_to(cov).parts
        if len(parts) >= 3:
            out.add(f"{parts[0]}/{parts[1]}")
    return out


def report(root: Path, show_gaps: bool = True) -> int:
    root = Path(root)
    if not root.exists():
        print(f"no existe el directorio de datos: {root}")
        return 1

    print(f"DATOS EN {root.resolve()}\n")
    counts = count_raw(root)
    connected = feeds_with_coverage(root)
    silent = sorted(f for f in connected if not counts.get(f))

    if not counts and not connected:
        print("  (todavía no hay ficheros crudos)")
    for feed in sorted(counts):
        total = sum(counts[feed].values())
        days = sorted(counts[feed])
        print(f"  {feed:<28} {total:>10,} registros   {len(days)} día(s)  {days[0]} -> {days[-1]}")

    # Esto es lo que no puede pasar desapercibido. Un feed que se conecta, se
    # mantiene, responde al keepalive y no graba NADA parece un mercado
    # tranquilo en un listado que solo enseña lo que sí tiene datos.
    for feed in silent:
        print(f"  {feed:<28} {0:>10,} registros   *** CONECTADO PERO SIN DATOS ***")
    if silent:
        print()
        print("  !! Un feed conectado que no graba nada NO es un mercado tranquilo.")
        print("     Casi siempre significa que la suscripción o el endpoint son")
        print("     incorrectos: el socket abre, el keepalive responde, y no llega")
        print("     un solo mensaje. Comprueba con:")
        print("         python -m liqrec.preflight")
        print("     que compara el stream contra uno de control de alta frecuencia.")

    if not show_gaps:
        return 1 if silent else 0

    print("\nCOBERTURA")
    cov_root = root / "coverage"
    if not cov_root.exists():
        print("  (sin registro de cobertura todavía)")
        return 0

    # Agrupar por FEED (exchange/stream), no por fichero individual: un
    # hueco real puede cruzar la medianoche (bug real 2026-09-17, ver
    # scan_gaps_across_files) y un solo fichero de un dia no tiene con que
    # compararlo en su propio borde. Los ficheros ya se llaman
    # AAAA-MM-DD.jsonl, asi que el orden alfabetico ya es cronologico.
    feeds_cov: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(cov_root.rglob("*.jsonl")):
        parts = path.relative_to(cov_root).parts
        if len(parts) < 3:
            continue
        feeds_cov[f"{parts[0]}/{parts[1]}"].append(path)

    for feed in sorted(feeds_cov):
        paths = feeds_cov[feed]
        gaps = scan_gaps_across_files(paths)
        real = [g for g in gaps if g.get("end") is None or (g["end"] - g["start"]) > 2000]
        label = f"{feed} ({len(paths)} dia(s))"
        if not real:
            print(f"  {label:<40} sin huecos")
            continue
        total_ms = sum((g["end"] - g["start"]) for g in real if g.get("end"))
        print(f"  {label:<40} {len(real)} hueco(s), {total_ms/1000:.0f}s sin cobertura")
        for g in real[:5]:
            # Fecha Y hora (no solo hora): un hueco fusionado entre feeds
            # puede empezar un dia y terminar otro, ver docstring de
            # scan_gaps_across_files -- solo la hora ya no basta para leerlo.
            a = datetime.fromtimestamp(g["start"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            b = ("abierto" if not g.get("end")
                 else datetime.fromtimestamp(g["end"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            print(f"      {a} -> {b}  ({g['reason']})")
        if len(real) > 5:
            print(f"      … y {len(real)-5} más")
    return 1 if silent else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="liqrec.doctor")
    ap.add_argument("--data-dir", default="./data")
    ap.add_argument("--no-gaps", action="store_true")
    a = ap.parse_args(argv)
    return report(Path(a.data_dir), show_gaps=not a.no_gaps)


if __name__ == "__main__":
    raise SystemExit(main())
