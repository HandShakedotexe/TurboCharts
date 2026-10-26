"""Los .ps1 tienen que ser ASCII puro. Este test existe por un fallo real.

Windows PowerShell 5.1 lee los .ps1 sin BOM como Windows-1252. Un guion largo
en UTF-8 (E2 80 94) se decodifica entonces como tres caracteres, y el tercero
(0x94) es la comilla tipografica de cierre, que PowerShell trata como
delimitador de cadena. Las comillas de la linea quedan impares, el parser se
descoloca y falla cincuenta lineas mas abajo, en un sitio sin relacion con la
causa.

Es imposible de detectar leyendo el script en un editor UTF-8, e imposible de
probar desde un entorno que no sea Windows. Un test que cuenta bytes si puede.
"""
import unittest
from pathlib import Path

# DOS raices, no una -- bug real (2026-09-17): este test
# solo escaneaba recorder/deploy, asi que los .ps1 nuevos de
# liq-intel/deploy/windows/ (run-api.ps1, run-web.ps1, los lanzadores, y
# ahora el backfill de big_trades) nunca pasaban por esta red de
# seguridad -- el mismo tipo de fallo (guion largo/acento que revienta
# PowerShell 5.1 cincuenta lineas mas abajo) podia colarse ahi sin que
# ningun test lo notara. `recorder/deploy` sigue primero por compatibilidad
# con quien ya usaba `DEPLOY` como nombre.
RECORDER_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
APP_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
DEPLOY = RECORDER_DEPLOY  # compat: algun otro sitio podria importar este nombre
DEPLOY_ROOTS = [RECORDER_DEPLOY, APP_DEPLOY]


def _all_ps1() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for root in DEPLOY_ROOTS:
        for path in sorted(root.rglob("*.ps1")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(path)
    return out


class TestPowerShellIsAscii(unittest.TestCase):
    def test_there_are_scripts_to_check(self):
        self.assertTrue(_all_ps1(), "no se encontro ningun .ps1")

    def test_every_ps1_is_pure_ascii(self):
        offenders = {}
        for path in _all_ps1():
            raw = path.read_bytes()
            bad = sorted({b for b in raw if b > 127})
            if bad:
                offenders[str(path)] = [hex(b) for b in bad]
        self.assertEqual(
            offenders, {},
            "estos .ps1 tienen bytes fuera de ASCII y PowerShell 5.1 los va a "
            "malinterpretar:\n" + "\n".join(f"  {k}: {v}" for k, v in offenders.items()),
        )

    def test_quotes_are_balanced_outside_comments(self):
        """Con ASCII puro las comillas ya no se descuadran solas, pero un
        desbalance escrito a mano rompe igual."""
        for path in _all_ps1():
            in_block = False
            count = 0
            for raw in path.read_text(encoding="ascii").splitlines():
                line = raw.strip()
                if line.startswith("<#"):
                    in_block = True
                if in_block:
                    if "#>" in line:
                        in_block = False
                    continue
                if line.startswith("#"):
                    continue
                count += raw.count('"')
            self.assertEqual(count % 2, 0,
                             f"{path.name}: numero impar de comillas dobles")


if __name__ == "__main__":
    unittest.main()
