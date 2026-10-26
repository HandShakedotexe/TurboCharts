"""`python -m liqrec.setup` — arranque guiado.

Esto vivía en un script de PowerShell y se movió aquí por un motivo concreto:
Windows PowerShell 5.1 lee los .ps1 sin BOM como Windows-1252, y entonces un
guión largo en UTF-8 se convierte en tres caracteres, uno de los cuales es la
comilla tipográfica `"`. PowerShell la trata como delimitador de cadena, las
comillas quedan impares y el parser revienta cincuenta líneas más abajo en un
sitio que no tiene nada que ver.

En Python no pasa nada de eso, y además se puede probar antes de dártelo.
Los .ps1 que quedan son lanzadores de tres líneas en ASCII puro.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from .config import Config
from .lock import AlreadyRunning, InstanceLock

BAR = "=" * 64


def section(n: str, title: str) -> None:
    print(f"\n{BAR}\n {n}. {title}\n{BAR}")


def fail(msg: str) -> int:
    print(f"\n  >> {msg}")
    return 2


def step_running(cfg: Config) -> bool:
    """Detecta otro grabador usando el mismo cerrojo que usa el grabador.

    Más fiable que buscar procesos por nombre: si el cerrojo está tomado, hay
    alguien escribiendo ahí, sea cual sea el nombre del proceso.
    """
    section("0", "Instancias en marcha")
    try:
        lock = InstanceLock(cfg.data_dir).acquire()
    except AlreadyRunning as exc:
        print(f"  Ya hay un grabador usando {cfg.data_dir}")
        print(f"  {exc.holder}")
        print()
        print("  Paralo antes de seguir. En PowerShell:")
        print("      Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |")
        print("        Where-Object { $_.CommandLine -match 'liqrec' } |")
        print("        Select-Object ProcessId, CommandLine")
        print("      Stop-Process -Id <PID>")
        return False
    lock.release()
    print("  Ninguna. Via libre.")
    return True


def step_python() -> bool:
    section("1", "Python")
    v = sys.version_info
    print(f"  {sys.executable}")
    print(f"  Python {v.major}.{v.minor}.{v.micro}")
    return v[:2] >= (3, 11)


def step_tests(repo: Path) -> bool:
    """Corre la suite y, si falla, ENSEÑA qué falló.

    La versión anterior imprimía solo las tres últimas líneas, o sea
    "FAILED (failures=2, errors=1)" y nada más. Una herramienta de diagnóstico
    que esconde el diagnóstico obliga a otra ronda de ida y vuelta para
    averiguar lo que ya sabía. Aquí cada ronda cuesta tiempo real de alguien,
    así que se imprime todo lo necesario a la primera.
    """
    section("2", "Tests (sin red, ~25 s)")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stderr or "") + (proc.stdout or "")
    lines = out.splitlines()

    summary = [l for l in lines if l.startswith(("Ran ", "OK", "FAILED"))]
    for l in summary:
        print(f"  {l}")
    if proc.returncode == 0:
        return True

    names = [l.split(":", 1)[1].strip() for l in lines if l.startswith(("FAIL:", "ERROR:"))]
    if names:
        print(f"\n  Fallan {len(names)}:")
        for n in names:
            print(f"    - {n}")

    print("\n  " + "-" * 60)
    print("  DETALLE (esto es lo que hay que mandarle a quien te dio el codigo)")
    print("  " + "-" * 60)
    started = False
    shown = 0
    for l in lines:
        if l.startswith("=" * 10) or l.startswith("-" * 10):
            started = True
        if started:
            print(f"  {l}")
            shown += 1
            if shown > 220:
                print("  ... (recortado)")
                break

    report = repo / "test-failures.txt"
    try:
        report.write_text(out, encoding="utf-8")
        print(f"\n  Salida completa guardada en: {report}")
    except OSError:
        pass
    return False


def step_config(config_path: Path) -> tuple[bool, Config | None]:
    section("3", "Tu configuracion")
    if not config_path.exists():
        print(f"  No existe {config_path}")
        return False, None
    try:
        cfg = Config.load(config_path)
    except Exception as exc:
        print(f"  No se pudo leer: {exc}")
        return False, None
    problems = cfg.validate()
    if problems:
        print(f"  {len(problems)} problema(s):\n")
        for p in problems:
            print(f"  - {p}")
        return False, cfg
    print(f"  {config_path}: sin problemas")
    print(f"  Datos en   : {cfg.data_dir}")
    print(f"  Aviso a    : {cfg.heartbeat_url or '(sin configurar)'}")
    return True, cfg


async def step_preflight(config_path: Path, seconds: float) -> bool:
    section("4", "Los exchanges desde esta conexion")
    from .preflight import run as preflight_run
    rc = await preflight_run(Path("./data"), seconds, config_path)
    return rc != 2


async def step_live(cfg: Config, config_path: Path, seconds: float) -> None:
    section("5", f"Prueba real de {seconds:.0f} segundos")
    from .__main__ import main_async
    stop = asyncio.Event()

    async def timer():
        await asyncio.sleep(seconds)
        stop.set()

    await asyncio.gather(main_async(cfg, None, stop), timer())

    section("6", "Que se grabo")
    from .doctor import report
    report(cfg.data_dir)


def next_steps(cfg: Config) -> None:
    section("7", "Dejarlo corriendo siempre")
    data = str(cfg.data_dir).replace("/", "\\")
    print("  Como ADMINISTRADOR (clic derecho en PowerShell -> Ejecutar como")
    print("  administrador), desde esta misma carpeta:\n")
    print("      powershell -ExecutionPolicy Bypass -File deploy\\windows\\harden.ps1")
    print(f'      powershell -ExecutionPolicy Bypass -File deploy\\windows\\install-task.ps1 -DataDir "{data}"')
    print()
    if not cfg.heartbeat_url:
        print("  ANTES DE ESO: pon tu URL de healthchecks.io en heartbeat_url.")
        print("  Sin eso, el dia que el grabador se pare no se entera nadie.")


async def run(repo: Path, config_path: Path, ws_seconds: float, live_seconds: float) -> int:
    """Corre TODOS los diagnósticos y da el veredicto al final.

    Deliberadamente no se para en el primer fallo. Cuando quien ejecuta esto
    está al otro lado de una conversación, cada parada temprana cuesta una
    ronda entera para descubrir algo que se podía haber medido en la misma
    pasada. Lo único que corta de raíz es un Python demasiado viejo, porque sin
    eso no se puede medir nada más.
    """
    print("\nGRABADOR DE LIQUIDACIONES - arranque guiado")
    print(f"Carpeta: {repo}")

    if not step_python():
        return fail("Se necesita Python 3.11 o superior. Lo demas no se puede comprobar sin eso.")

    problems = []
    if not step_tests(repo):
        problems.append("los tests fallan")

    ok, cfg = step_config(config_path)
    if not ok or cfg is None:
        problems.append("la configuracion tiene errores")
    if cfg is not None:
        if not step_running(cfg):
            problems.append("hay otro grabador en marcha")
        if not await step_preflight(config_path, ws_seconds):
            problems.append("la red o los endpoints no responden")

    if problems:
        section("!", "Veredicto")
        print(f"  No se arranca todavia. {len(problems)} cosa(s) que resolver:\n")
        for p in problems:
            print(f"    - {p}")
        print("\n  El detalle de cada una esta mas arriba.")
        return 2

    await step_live(cfg, config_path, live_seconds)
    next_steps(cfg)
    print()
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="liqrec.setup")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--ws-seconds", type=float, default=15.0)
    ap.add_argument("--live-seconds", type=float, default=60.0)
    a = ap.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        return asyncio.run(run(repo, Path(a.config), a.ws_seconds, a.live_seconds))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
