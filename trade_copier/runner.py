"""Process orchestration.

The MetaTrader5 Python package can only attach to one terminal per process, so:

* the main process watches the master terminal and publishes snapshots;
* each slave runs in its own process with its own terminal, receiving the
  latest snapshot through a queue and reconciling against it.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue as queue_mod
import re
import time
from pathlib import Path

from .broker import BrokerError, MT5Broker
from .config import Config, MasterConfig, Settings, SlaveConfig
from .copier import SlaveCopier
from .logsetup import setup_logging
from .master import MasterWatcher, fingerprint
from .state import SlaveState
from .status import install_stop_signals, write_status

RECONNECT_SECONDS = 10.0
STATUS_EVERY_SECONDS = 2.0


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _connect_with_retry(broker: MT5Broker, log: logging.Logger, stop, report=None) -> bool:
    while not stop.is_set():
        try:
            broker.connect()
            acct = broker.account()
            log.info("connected to account %s (%s, balance %.2f)", acct.login, acct.currency, acct.balance)
            return True
        except BrokerError as exc:
            log.error("%s; retrying in %.0fs", exc, RECONNECT_SECONDS)
            if report:
                report(state="connecting", error=str(exc))
            broker.shutdown()
            stop.wait(RECONNECT_SECONDS)
    return False


def _account_status(broker: MT5Broker) -> dict:
    acct = broker.account()
    if acct is None:
        return {"connected": False}
    return {
        "connected": broker.is_connected(),
        "login": acct.login,
        "currency": acct.currency,
        "balance": acct.balance,
        "equity": acct.equity,
    }


def slave_main(cfg: SlaveConfig, master_cfg: MasterConfig, settings: Settings, q, stop) -> None:
    install_stop_signals()
    log = setup_logging(cfg.name, settings.log_dir, settings.log_level)
    try:
        _slave_loop(cfg, master_cfg, settings, q, stop, log)
    except KeyboardInterrupt:  # Ctrl+C reaches every process in the console
        pass
    write_status(settings.state_dir, cfg.name, {"role": "slave", "state": "stopped"})


def _slave_loop(cfg, master_cfg, settings, q, stop, log) -> None:
    def report(**data):
        write_status(settings.state_dir, cfg.name, {"role": "slave", "dry_run": cfg.dry_run, **data})

    report(state="connecting")
    broker = MT5Broker(cfg, label=cfg.name)
    if not _connect_with_retry(broker, log, stop, report):
        return
    if not broker.algo_trading_enabled():
        log.warning("'Algo Trading' is switched OFF in this terminal: orders will be rejected until you enable it")
    if cfg.dry_run:
        log.warning("DRY RUN: trades are logged but not sent")

    state = SlaveState.load(Path(settings.state_dir) / f"{_safe(cfg.name)}.json")
    copier = SlaveCopier(cfg, master_cfg, broker, state, logger=log)
    last_snapshot_at = time.time()
    stale_warned = False
    last_status = 0.0

    while not stop.is_set():
        if time.time() - last_status >= STATUS_EVERY_SECONDS:
            last_status = time.time()
            report(
                state="running",
                algo_trading=broker.algo_trading_enabled(),
                copied_trades=sum(1 for link in state.links.values() if link.status == "open"),
                master_feed=time.time() - last_snapshot_at < 15,
                **_account_status(broker),
            )
        try:
            snap = q.get(timeout=1.0)
        except queue_mod.Empty:
            snap = None
        # Drain to the newest snapshot; older ones are obsolete.
        while True:
            try:
                snap = q.get_nowait()
            except queue_mod.Empty:
                break

        if snap is None:
            if time.time() - last_snapshot_at > 15 and not stale_warned:
                log.warning("no data from the master for a while; holding all trades as they are")
                stale_warned = True
            continue
        if time.time() - snap.created > settings.max_snapshot_age_seconds:
            continue
        last_snapshot_at, stale_warned = time.time(), False

        if not broker.is_connected():
            log.warning("slave terminal lost connection to the broker; waiting")
            stop.wait(RECONNECT_SECONDS)
            continue
        try:
            copier.process(snap)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("unexpected error while copying")
            stop.wait(1.0)
    state.save(force=True)
    broker.shutdown()
    log.info("stopped")


def run(config: Config) -> None:
    install_stop_signals()
    settings = config.settings
    log = setup_logging(config.master.name, settings.log_dir, settings.log_level)
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    slaves = config.enabled_slaves
    if not slaves:
        log.error("no enabled slaves in the config")
        return

    workers: dict = {}

    def start(cfg: SlaveConfig):
        q = ctx.Queue(maxsize=5)
        p = ctx.Process(
            target=slave_main, args=(cfg, config.master, settings, q, stop), name=cfg.name, daemon=True
        )
        p.start()
        workers[cfg.name] = {"cfg": cfg, "q": q, "p": p, "restarts": 0, "next_restart": 0.0}
        log.info("started slave '%s' (pid %s)", cfg.name, p.pid)

    for cfg in slaves:
        start(cfg)

    broker = MT5Broker(config.master, label=config.master.name)
    watcher = MasterWatcher(broker, config.master)
    interval = settings.poll_interval_ms / 1000.0
    last_fp, last_sent, last_bad_warn, last_status = None, 0.0, 0.0, 0.0

    def report(**data):
        write_status(settings.state_dir, config.master.name, {"role": "master", **data})

    try:
        report(state="connecting")
        if not _connect_with_retry(broker, log, stop, report):
            return
        log.info("watching the master; copying to %d slave(s). Press Ctrl+C to stop.", len(slaves))
        while True:
            snap = watcher.snapshot()
            now = time.time()
            if snap is None:
                if now - last_bad_warn > 30:
                    log.warning("could not read the master terminal (disconnected?); slaves will hold")
                    last_bad_warn = now
                if not broker.is_connected():
                    report(state="connecting", connected=False)
                    broker.shutdown()
                    _connect_with_retry(broker, log, stop, report)
            else:
                fp = fingerprint(snap)
                if fp != last_fp or now - last_sent >= settings.heartbeat_seconds:
                    if fp != last_fp:
                        log.debug("master changed: %d position(s), %d pending order(s)",
                                  len(snap.positions), len(snap.orders))
                    for w in workers.values():
                        _publish(w["q"], snap)
                    last_fp, last_sent = fp, now
                if now - last_status >= STATUS_EVERY_SECONDS:
                    last_status = now
                    report(state="running", positions=len(snap.positions), pending_orders=len(snap.orders),
                           connected=True, login=snap.account.login, currency=snap.account.currency,
                           balance=snap.account.balance, equity=snap.account.equity)

            for name, w in list(workers.items()):
                if not w["p"].is_alive() and now >= w["next_restart"]:
                    w["restarts"] += 1
                    delay = min(300, 5 * 2 ** min(w["restarts"], 6))
                    log.error("slave '%s' process stopped (exit %s); restarting", name, w["p"].exitcode)
                    restarts = w["restarts"]
                    start(w["cfg"])
                    workers[name]["restarts"] = restarts
                    workers[name]["next_restart"] = now + delay
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopping...")
    finally:
        stop.set()
        for w in workers.values():
            w["p"].join(timeout=10)
        report(state="stopped")
        broker.shutdown()
        log.info("stopped")


def _publish(q, snap) -> None:
    try:
        q.put_nowait(snap)
    except queue_mod.Full:
        try:
            q.get_nowait()
        except queue_mod.Empty:
            pass
        try:
            q.put_nowait(snap)
        except queue_mod.Full:
            pass
