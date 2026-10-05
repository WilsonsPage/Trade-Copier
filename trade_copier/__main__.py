"""Command line entry point.

    python -m trade_copier validate   check the config file
    python -m trade_copier check      connect to every terminal and report
    python -m trade_copier run        start copying
    python -m trade_copier ui         open the control panel in your browser
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, load_config


def cmd_validate(config) -> int:
    print(f"Config OK: master '{config.master.name}', {len(config.enabled_slaves)} enabled slave(s)")
    for s in config.slaves:
        lots = s.lots
        detail = {
            "multiplier": f"x{lots.multiplier}",
            "balance_ratio": f"by balance ratio x{lots.multiplier}",
            "equity_ratio": f"by equity ratio x{lots.multiplier}",
            "fixed": f"fixed {lots.fixed_lot} lots",
            "risk_percent": f"{lots.risk_percent}% risk per trade",
        }[lots.mode]
        flags = [f for f, on in (("reverse", s.reverse), ("dry run", s.dry_run), ("disabled", not s.enabled)) if on]
        print(f"  - {s.name}: lots {detail}" + (f" [{', '.join(flags)}]" if flags else ""))
    return 0


def cmd_check(config) -> int:
    from .broker import BrokerError, MT5Broker
    from .fx import conversion_rate
    from .symbols import SymbolMapper

    ok = True
    master_ccy = None
    master_symbols: set = set()
    for label, term in [(config.master.name, config.master), *[(s.name, s) for s in config.enabled_slaves]]:
        print(f"\n== {label} ({term.terminal_path})")
        broker = MT5Broker(term, label=label)
        try:
            broker.connect()
        except BrokerError as exc:
            print(f"  FAILED: {exc}")
            ok = False
            continue
        try:
            a = broker.account()
            mode = {0: "netting", 1: "exchange", 2: "hedging"}.get(a.margin_mode, a.margin_mode)
            print(f"  account {a.login}  {a.currency}  balance {a.balance:.2f}  equity {a.equity:.2f}  ({mode})")
            print(f"  broker connection: {'OK' if broker.is_connected() else 'NOT CONNECTED'}")
            if term is config.master:
                master_ccy = a.currency
                for p in broker.positions() or []:
                    master_symbols.add(p.symbol)
                for o in broker.orders() or []:
                    master_symbols.add(o.symbol)
                continue
            algo = broker.algo_trading_enabled()
            print(f"  Algo Trading button: {'ON' if algo else 'OFF  <-- turn it on or orders will be rejected'}")
            ok &= algo
            if a.margin_mode != 2:
                print("  note: this is not a hedging account; multiple trades on one symbol will merge")
            mapper = SymbolMapper(term.symbols, config.master.symbol_prefix, config.master.symbol_suffix)
            if master_ccy and master_ccy != a.currency:
                rate = conversion_rate(master_ccy, a.currency, broker.tick, mapper.fx_symbol, term.fx_rates)
                if rate is None:
                    print(f"  {master_ccy}->{a.currency}: NO RATE FOUND (add fx_rates if using balance/equity ratio)")
                    ok &= term.lots.mode not in ("balance_ratio", "equity_ratio")
                else:
                    print(f"  {master_ccy}->{a.currency} rate: {rate:.5f}")
            to_check = set(term.symbols.map) | set(term.symbols.include) | master_symbols
            for sym in sorted(to_check):
                target = mapper.to_slave(sym)
                spec = broker.symbol_info(target)
                status = (f"OK (min {spec.volume_min}, step {spec.volume_step}, contract {spec.contract_size})"
                          if spec else "NOT FOUND <-- add it to symbols.map")
                ok &= spec is not None
                print(f"  {sym} -> {target}: {status}")
        finally:
            broker.shutdown()
    print("\nAll checks passed." if ok else "\nSome checks failed; see above.")
    return 0 if ok else 1


def cmd_run(config) -> int:
    from .runner import run

    run(config)
    return 0


def cmd_ui(args) -> int:
    from .ui.server import serve

    serve(args.config, port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trade_copier", description="MetaTrader 5 trade copier")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=["validate", "check", "run", "ui"])
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config file (default config.yaml)")
    parser.add_argument("--port", type=int, default=8765, help="control panel port (ui only)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser (ui only)")
    args = parser.parse_args(argv)
    if args.command == "ui":  # the panel works even before a config file exists
        return cmd_ui(args)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return {"validate": cmd_validate, "check": cmd_check, "run": cmd_run}[args.command](config)


if __name__ == "__main__":
    sys.exit(main())
