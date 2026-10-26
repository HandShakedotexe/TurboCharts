"""`python -m liqrec.preflight` — comprobación previa al arranque.

Contesta, desde la máquina donde va a correr el grabador, las cuatro preguntas
que no se pueden contestar desde ningún otro sitio:

  1. ¿Hay un Python capaz de ejecutar esto?
  2. ¿Responden los exchanges DESDE ESTA CONEXIÓN? (geobloqueos, DNS del ISP,
     firewall corporativo, proxy...)
  3. ¿Llegan mensajes de verdad por WebSocket, o solo abre el socket?
  4. ¿Hay disco suficiente para el primer año?

Usa el mismo cliente WebSocket que el grabador, así que si el preflight pasa,
el grabador conecta.

Comprueba las URLs QUE TIENES CONFIGURADAS, no las que deberían ser. La
diferencia importa: un preflight que prueba la URL correcta mientras el
grabador usa otra da luz verde a un sistema roto, que es justo lo que pasó con
el endpoint viejo de Binance.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import ssl
import sys
import time
from pathlib import Path

from .config import Config
from .rest import HttpError, get_json
from .wsmin import WSClient, WSError

OK, WARN, BAD = "  OK  ", " AVISO", " FALLO"

# Estimación del propio proyecto: ~76 MB/día comprimidos con la configuración
# por defecto, x2 de margen por días de alta volatilidad.
GB_PER_YEAR = 55


def line(status: str, text: str, detail: str = "") -> bool:
    print(f"  [{status}] {text}" + (f"   {detail}" if detail else ""))
    return status == OK


# --------------------------------------------------------------- 1. Python
def check_python() -> bool:
    v = sys.version_info
    if v[:2] >= (3, 11):
        return line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    line(BAD, f"Python {v.major}.{v.minor} — se necesita 3.11 o superior")
    return False


# --------------------------------------------------------- 1b. Configuración
# Endpoints que el exchange ya desmanteló. La conexión se abre igual y no llega
# nunca un mensaje, así que hay que detectarlos leyendo la URL, no probándola.
STALE_ENDPOINTS = [
    (lambda u: "fstream.binance.com" in u and "/market/" not in u,
     "Binance desmanteló este endpoint el 2026-04-23. !forceOrder@arr es dato "
     "de 'market': la URL tiene que ser wss://fstream.binance.com/market/... "
     "Con la antigua el socket abre y NO llega jamás un mensaje."),
]


def check_config(cfg: Config, path: Path) -> bool:
    ok = True
    line(OK, f"Config", str(path))
    line(OK, "Datos en", str(cfg.data_dir))

    for name in ("binance", "bybit", "okx"):
        x = getattr(cfg, name)
        if not x.enabled:
            line(WARN, f"{name:<14} deshabilitado en la configuración")
            continue
        problem = next((msg for test, msg in STALE_ENDPOINTS if test(x.ws_url)), None)
        if problem:
            line(BAD, f"{name:<14} URL", x.ws_url)
            print(f"          -> {problem}")
            ok = False
        else:
            line(OK, f"{name:<14} URL", x.ws_url)
    return ok


async def check_heartbeat(cfg: Config) -> bool:
    """Comprueba que la URL de aviso existe y responde.

    Se llama a `<url>/log`, que registra un evento SIN tocar el estado del
    check. Un ping normal marcaría "todo bien" en el monitor mientras aquí no
    hay nada corriendo — justo la mentira que este sistema existe para evitar.

    IMPORTANTE (confirmado 2026-09-17): este chequeo solo
    prueba que la URL de ping es VALIDA y responde -- NUNCA puede saber si
    el "Period"/"Grace Time" configurados EN healthchecks.io (en su propia
    web, cuenta del usuario) coinciden con `heartbeat_seconds_interval`. La
    URL de ping (`hc-ping.com/<uuid>`) no da acceso de lectura a esa
    configuracion -- solo la Management API de healthchecks.io (una API key
    de cuenta, que este proyecto no guarda en ningun lado) podria leerla o
    cambiarla. Un check con el "Period" en el default de la web (a menudo
    1 dia) en vez de unos pocos minutos puede dejar pasar un corte de horas
    sin avisar a nadie, con esta comprobacion diciendo igual "la URL
    responde" -- confirmado con un corte real de 30.3h que no se sabe si
    disparo alerta o no, porque nadie habia verificado esa configuracion
    hasta ahora."""
    if not cfg.heartbeat_url:
        line(WARN, "Heartbeat", "sin configurar — si el grabador se para, no se entera nadie")
        return True   # arrancable, pero a ciegas
    try:
        await get_json(cfg.heartbeat_url.rstrip("/") + "/log", timeout=10)
    except HttpError as exc:
        if exc.status in (200, 201):
            pass
        elif exc.status == 404:
            line(BAD, "Heartbeat", "404 — la URL de ping no existe o está mal copiada")
            return False
        else:
            line(WARN, "Heartbeat", f"respondió HTTP {exc.status} a /log (¿no es healthchecks.io?)")
            return True
    except ValueError:
        pass   # respondió "OK" en texto plano, no JSON: es lo normal
    except Exception as exc:
        line(WARN, "Heartbeat", f"no se pudo contactar ({type(exc).__name__})")
        return True
    line(OK, "Heartbeat", f"la URL responde · aviso cada {cfg.heartbeat_seconds_interval:.0f}s")
    print(f"          -> Esto SOLO prueba que la URL responde. Entra a healthchecks.io y")
    print(f"             confirma a mano que \"Period\" ronda los {cfg.heartbeat_seconds_interval:.0f}s")
    print(f"             (no el default de la web, que suele ser 1 dia) y que \"Grace Time\"")
    print(f"             es de unos pocos minutos -- si no, un corte de horas puede pasar")
    print(f"             sin avisar aunque este chequeo diga \"OK\".")
    return True


# ----------------------------------------------------------------- 2. REST
REST_CHECKS = [
    ("Binance USD-M", "https://fapi.binance.com/fapi/v1/time"),
    ("Bybit v5",      "https://api.bybit.com/v5/market/time"),
    ("OKX v5",        "https://www.okx.com/api/v5/public/time"),
]


async def check_rest(cfg: Config = None) -> bool:
    checks = REST_CHECKS
    if cfg is not None:
        checks = [
            ("Binance USD-M", f"{cfg.binance.rest_url}/fapi/v1/time"),
            ("Bybit v5",      f"{cfg.bybit.rest_url}/v5/market/time"),
            ("OKX v5",        f"{cfg.okx.rest_url}/api/v5/public/time"),
        ]
    all_ok = True
    for name, url in checks:
        t0 = time.monotonic()
        try:
            await get_json(url, timeout=12)
            ms = (time.monotonic() - t0) * 1000
            tag = OK if ms < 1500 else WARN
            line(tag, f"{name:<14} REST", f"{ms:6.0f} ms")
            all_ok &= tag == OK
        except HttpError as exc:
            if exc.status == 451:
                line(BAD, f"{name:<14} REST", "HTTP 451 — esta IP está geobloqueada")
            else:
                line(BAD, f"{name:<14} REST", f"HTTP {exc.status}")
            all_ok = False
        except Exception as exc:
            line(BAD, f"{name:<14} REST", f"{type(exc).__name__}: {exc}"[:90])
            all_ok = False
    return all_ok


# ------------------------------------------------------------ 3. WebSocket
# Cada comprobación abre UNA conexión y se suscribe a dos cosas: el stream de
# liquidaciones que nos interesa y un stream de control de alta frecuencia
# (trades). Sin el control, "0 mensajes" es ambiguo: puede ser un mercado
# tranquilo o una suscripción rota. Con el control deja de serlo.
#
#   control > 0, liquidaciones > 0  -> todo bien
#   control > 0, liquidaciones = 0  -> conexión sana, no hubo liquidaciones
#   control = 0                     -> FALLO: el endpoint no entrega nada
#
# Esta distinción no es teórica: la URL antigua de Binance aceptaba la conexión
# y no enviaba jamás un mensaje, y así estuvo dos días pareciendo normal.

def _kind_binance(msg: dict) -> str:
    stream = msg.get("stream", "")
    if "forceOrder" in stream:
        return "target"
    if "aggTrade" in stream:
        return "control"
    return "otro"


def _kind_bybit(msg: dict) -> str:
    topic = msg.get("topic", "")
    if topic.startswith("allLiquidation"):
        return "target"
    if topic.startswith("publicTrade"):
        return "control"
    return "otro"


def _kind_okx(msg: dict) -> str:
    channel = (msg.get("arg") or {}).get("channel", "")
    if channel == "liquidation-orders":
        return "target"
    if channel == "trades":
        return "control"
    return "otro"


WS_CHECKS = [
    ("Binance USD-M",
     "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr/btcusdt@aggTrade",
     None, _kind_binance),
    ("Bybit v5",
     "wss://stream.bybit.com/v5/public/linear",
     {"op": "subscribe", "args": ["allLiquidation.BTCUSDT", "allLiquidation.ETHUSDT",
                                  "allLiquidation.SOLUSDT", "publicTrade.BTCUSDT"]},
     _kind_bybit),
    ("OKX v5",
     "wss://ws.okx.com:8443/ws/v5/public",
     {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"},
                                  {"channel": "trades", "instId": "BTC-USDT-SWAP"}]},
     _kind_okx),
]


def _configured_ws_checks(cfg: Config):
    """Las mismas comprobaciones, pero contra las URLs del fichero de config.

    Al stream de liquidaciones se le añade uno de trades como control. En
    Binance el control va en la propia URL combinada; en Bybit y OKX, en la
    suscripción.
    """
    binance_url = cfg.binance.ws_url
    if "streams=" in binance_url and "aggTrade" not in binance_url:
        binance_url += "/btcusdt@aggTrade"
    return [
        ("Binance USD-M", binance_url, None, _kind_binance),
        ("Bybit v5", cfg.bybit.ws_url, WS_CHECKS[1][2], _kind_bybit),
        ("OKX v5", cfg.okx.ws_url, WS_CHECKS[2][2], _kind_okx),
    ]


async def check_ws(seconds: float, cfg: Config = None) -> bool:
    checks = _configured_ws_checks(cfg) if cfg is not None else WS_CHECKS
    all_ok = True
    for name, url, sub, classify in checks:
        t0 = time.monotonic()
        ws = None
        counts = {"target": 0, "control": 0, "otro": 0}
        try:
            ws = await WSClient.connect(url, open_timeout=15)
            handshake_ms = (time.monotonic() - t0) * 1000
            if sub:
                await ws.send(json.dumps(sub))

            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    raw = await ws.recv(timeout=max(0.5, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict):
                    counts[classify(msg)] += 1

            detail = f"handshake {handshake_ms:5.0f} ms · {counts['control']} trades · {counts['target']} liquidaciones"
            if counts["control"] == 0:
                line(BAD, f"{name:<14} WS  ", detail)
                print(f"          -> la conexión no entrega NINGÚN dato en {seconds:.0f}s.")
                print(f"             No es mercado tranquilo: el stream de trades tampoco llega.")
                print(f"             Revisa que la URL sea el endpoint correcto del exchange.")
                all_ok = False
            elif counts["target"] == 0:
                line(OK, f"{name:<14} WS  ", detail)
                print(f"          -> conexión sana; no hubo liquidaciones en la ventana (normal)")
            else:
                line(OK, f"{name:<14} WS  ", detail)
        except WSError as exc:
            line(BAD, f"{name:<14} WS  ", str(exc)[:90])
            all_ok = False
        except Exception as exc:
            line(BAD, f"{name:<14} WS  ", f"{type(exc).__name__}: {exc}"[:90])
            all_ok = False
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
    return all_ok


# ---------------------------------------------------------------- 4. Disco
def check_disk(data_dir: Path) -> bool:
    probe = data_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free_gb = shutil.disk_usage(probe).free / 1e9
    except OSError as exc:
        return line(WARN, "Disco", f"no se pudo medir ({exc})")
    if free_gb >= GB_PER_YEAR * 2:
        return line(OK, "Disco", f"{free_gb:.0f} GB libres en {probe} (~{GB_PER_YEAR} GB/año)")
    if free_gb >= GB_PER_YEAR:
        return line(WARN, "Disco", f"{free_gb:.0f} GB libres — da para ~1 año, vigílalo")
    line(BAD, "Disco", f"solo {free_gb:.0f} GB libres — se estiman {GB_PER_YEAR} GB el primer año")
    return False


# ------------------------------------------------------------------- main
async def run(data_dir: Path, seconds: float, config_path: Path = None) -> int:
    print("\n=== PREFLIGHT DEL GRABADOR ===\n")
    print(" 1. Entorno")
    py = check_python()

    cfg = None
    conf_ok = True
    if config_path and config_path.exists():
        print("\n 1b. Lo que dice tu configuración")
        try:
            cfg = Config.load(config_path)
            if data_dir == Path("./data"):
                data_dir = cfg.data_dir      # el config manda si no se pidió otra cosa
            conf_ok = check_config(cfg, config_path)
            conf_ok &= await check_heartbeat(cfg)
        except Exception as exc:
            line(BAD, "Config", f"no se pudo leer {config_path}: {exc}"[:110])
            cfg, conf_ok = None, False
    else:
        print("\n 1b. Configuración")
        line(WARN, "Config", "sin fichero — se prueban las URLs por defecto, no las tuyas")

    print("\n 2. ¿Responden los exchanges desde esta conexión?")
    rest = await check_rest(cfg)
    print(f"\n 3. ¿Llegan mensajes de verdad? (escuchando {seconds:.0f}s por exchange)")
    ws = await check_ws(seconds, cfg)
    print("\n 4. Espacio en disco")
    disk = check_disk(data_dir)

    print("\n" + "-" * 62)
    if py and conf_ok and rest and ws and disk:
        print("  TODO LISTO. Puedes arrancar el grabador.")
        return 0
    if not py:
        print("  BLOQUEADO: instala Python 3.11 o superior.")
        return 2
    if not conf_ok:
        print("  BLOQUEADO: la configuración apunta a algo que no va a funcionar.")
        print("  Arregla las líneas marcadas arriba en tu config.toml y repite.")
        return 2
    if not rest or not ws:
        print("  BLOQUEADO: algún exchange no es alcanzable desde esta red.")
        print("  Si ves un 451, esta IP está geobloqueada y hay que grabar desde otro sitio.")
        print("  Si es un timeout, revisa firewall, antivirus o proxy antes de seguir.")
        return 2
    print("  ARRANCABLE, pero revisa los avisos de arriba.")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="liqrec.preflight")
    ap.add_argument("--config", default="config.toml",
                    help="fichero a validar (por defecto config.toml del directorio actual)")
    ap.add_argument("--data-dir", default="./data")
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="cuánto escuchar cada WebSocket (por defecto 15)")
    a = ap.parse_args(argv)
    try:
        return asyncio.run(run(Path(a.data_dir), a.seconds, Path(a.config)))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
