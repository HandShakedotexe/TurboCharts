"""Punto de entrada del recorder.

    python -m liqrec --config config.toml

Arranca un supervisor por feed de liquidaciones, los pollers REST y el
publicador de estado, y se apaga limpiamente con SIGINT/SIGTERM cerrando y
comprimiendo los ficheros abiertos.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import __version__, pollers
from .adapters import BinanceAdapter, BybitAdapter, OkxAdapter
from .config import Config, ConfigError
from .coverage import CoverageLog
from .health import Health, serve_health
from .lock import AlreadyRunning, InstanceLock
from .heartbeat import Heartbeat
from .writer import RawWriter, utc_ms

log = logging.getLogger("liqrec")

ADAPTERS = {"binance": BinanceAdapter, "bybit": BybitAdapter, "okx": OkxAdapter}


def build(cfg: Config):
    writers, feeds, poller_list = [], [], []

    for name, cls in ADAPTERS.items():
        xcfg = getattr(cfg, name)
        if not xcfg.enabled:
            log.info("[%s] deshabilitado por configuración", name)
            continue
        w = RawWriter(
            cfg.data_dir, name, "liquidation",
            fsync_seconds=cfg.fsync_seconds,
            compress_on_rotate=cfg.compress_on_rotate,
        )
        cov = CoverageLog(cfg.data_dir, name, "liquidation")
        writers.append(w)
        feeds.append(cls(cfg, xcfg, w, cov))

    def add_poller(exchange: str, stream: str, fetch, interval: float):
        w = RawWriter(
            cfg.data_dir, exchange, stream,
            fsync_seconds=cfg.fsync_seconds,
            compress_on_rotate=cfg.compress_on_rotate,
        )
        writers.append(w)
        poller_list.append(pollers.Poller(f"{exchange}/{stream}", w, fetch, interval))

    if cfg.binance.enabled:
        add_poller("binance", "premium_index",
                   pollers.binance_premium_index(cfg.binance.rest_url), cfg.funding_poll_seconds)
        syms = cfg.binance.poll_symbols or cfg.top_symbols
        if syms:
            add_poller("binance", "open_interest",
                       pollers.binance_open_interest(cfg.binance.rest_url, syms), cfg.oi_poll_seconds)
    if cfg.bybit.enabled:
        # tickers trae openInterest + fundingRate + markPrice de todo el mercado
        add_poller("bybit", "tickers", pollers.bybit_tickers(cfg.bybit.rest_url), cfg.oi_poll_seconds)
    if cfg.okx.enabled:
        add_poller("okx", "open_interest", pollers.okx_open_interest(cfg.okx.rest_url), cfg.oi_poll_seconds)
        add_poller("okx", "mark_price", pollers.okx_mark_price(cfg.okx.rest_url), cfg.funding_poll_seconds)
        syms = cfg.okx.poll_symbols
        if syms:
            add_poller("okx", "funding_rate",
                       pollers.okx_funding(cfg.okx.rest_url, syms), cfg.funding_poll_seconds)

    add_poller("reference", "instruments",
               pollers.reference_snapshot(cfg.binance.rest_url, cfg.bybit.rest_url, cfg.okx.rest_url),
               cfg.reference_refresh_seconds)

    return writers, feeds, poller_list


async def main_async(cfg: Config, health_port: int | None = None, stop: asyncio.Event | None = None) -> int:
    started = utc_ms()

    # Antes que nada: nadie más puede estar escribiendo aquí.
    try:
        lock = InstanceLock(cfg.data_dir).acquire()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 3

    writers, feeds, poller_list = build(cfg)
    if not feeds:
        log.error("no hay ningún feed habilitado; revisa la configuración")
        lock.release()
        return 2

    def snapshot() -> dict:
        return {
            "version": __version__,
            "started_at": started,
            "now": utc_ms(),
            "uptime_seconds": (utc_ms() - started) // 1000,
            "data_dir": str(cfg.data_dir.resolve()),
            "feeds": [f.status() for f in feeds],
            "pollers": [p.status() for p in poller_list],
            "heartbeat": beat.status(),
        }

    beat = Heartbeat(
        cfg.heartbeat_url, snapshot,
        interval=cfg.heartbeat_seconds_interval,
        require_all_feeds=cfg.heartbeat_require_all_feeds,
    )
    health = Health(cfg.health_file, snapshot)
    tasks = [asyncio.create_task(f.run(), name=f"feed:{f.name}") for f in feeds]
    tasks += [asyncio.create_task(p.run(), name=f"poll:{p.name}") for p in poller_list]
    tasks.append(asyncio.create_task(health.run(), name="health"))
    tasks.append(asyncio.create_task(beat.run(), name="heartbeat"))

    server = None
    if health_port:
        try:
            server = await serve_health("0.0.0.0", health_port, snapshot)
        except OSError as exc:
            # Un puerto ocupado no puede tumbar al grabador: health.json sigue
            # escribiéndose y el heartbeat sigue funcionando. Lo único que se
            # pierde es poder consultar el estado por HTTP.
            log.warning("no se pudo abrir el puerto de health %d (%s); "
                        "el grabador sigue, pero sin endpoint HTTP", health_port, exc)

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop.set())   # Windows

    log.info("recorder en marcha · %d feeds · %d pollers · datos en %s",
             len(feeds), len(poller_list), cfg.data_dir.resolve())
    await stop.wait()
    log.info("apagando…")

    for f in feeds:
        f.stop()
    for p in poller_list:
        p.stop()
    health.stop()
    beat.stop()
    if server is not None:
        server.close()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    for w in writers:
        try:
            w.close()
        except Exception:
            log.exception("error cerrando %s/%s", w.exchange, w.stream)
    for f in feeds:
        f.coverage.mark_down(reason="shutdown")
        f.coverage.close()
    health.write_once()
    lock.release()
    log.info("apagado limpio")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="liqrec", description="Recorder de liquidaciones (Fase 0)")
    ap.add_argument("--config", default="config.toml", help="ruta al fichero TOML")
    ap.add_argument("--data-dir", default=None, help="sobrescribe recorder.data_dir")
    ap.add_argument("--health-port", type=int, default=None, help="expone GET /health en este puerto")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)sZ %(levelname)-7s %(name)s · %(message)s",
    )
    logging.Formatter.converter = __import__("time").gmtime

    path = Path(args.config)
    if not path.exists():
        log.error("no existe el fichero de configuración: %s", path)
        return 2
    cfg = Config.load(path)
    problems = cfg.validate()
    if problems:
        log.error("la configuración tiene %d problema(s); no arranco así:", len(problems))
        for prob in problems:
            log.error("  - %s", prob)
        return 2
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir)
        cfg.health_file = cfg.data_dir / "health.json"

    try:
        return asyncio.run(main_async(cfg, args.health_port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
