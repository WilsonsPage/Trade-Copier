"""Access to a MetaTrader 5 terminal.

The ``MetaTrader5`` Python package talks to exactly one terminal per process,
which is why the runner gives every account its own process.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from .config import TerminalConfig
from .constants import PENDING_TYPES
from .models import AccountSnap, OrderSnap, PositionSnap, SendResult, SymbolSpec, Tick

log = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


class Broker(Protocol):
    def connect(self) -> None: ...
    def shutdown(self) -> None: ...
    def is_connected(self) -> bool: ...
    def account(self) -> Optional[AccountSnap]: ...
    def positions(self) -> Optional[list]: ...
    def orders(self) -> Optional[list]: ...
    def symbol_info(self, name: str) -> Optional[SymbolSpec]: ...
    def tick(self, name: str) -> Optional[Tick]: ...
    def send(self, request: dict) -> SendResult: ...


def _import_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - only on non-Windows
        raise BrokerError(
            "The MetaTrader5 package is not installed. It only runs on Windows: "
            "pip install -r requirements.txt"
        ) from exc
    return mt5


class MT5Broker:
    def __init__(self, terminal: TerminalConfig, label: str = ""):
        self.terminal = terminal
        self.label = label or terminal.terminal_path
        self.mt5 = None

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        mt5 = self.mt5 = _import_mt5()
        kwargs = {"path": self.terminal.terminal_path, "portable": self.terminal.portable, "timeout": 60_000}
        if self.terminal.login:
            kwargs.update(login=int(self.terminal.login), password=self.terminal.password, server=self.terminal.server)
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            mt5.shutdown()
            raise BrokerError(f"[{self.label}] could not start/attach to MT5 terminal: {err}")
        info = mt5.account_info()
        if info is None:
            raise BrokerError(f"[{self.label}] terminal is not logged in to an account: {mt5.last_error()}")
        if self.terminal.login and info.login != int(self.terminal.login):
            raise BrokerError(
                f"[{self.label}] terminal is logged in to {info.login}, expected {self.terminal.login}"
            )

    def shutdown(self) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()

    def is_connected(self) -> bool:
        if self.mt5 is None:
            return False
        info = self.mt5.terminal_info()
        return bool(info is not None and info.connected)

    def algo_trading_enabled(self) -> bool:
        info = self.mt5.terminal_info() if self.mt5 else None
        return bool(info is not None and info.trade_allowed)

    # -- reads ------------------------------------------------------------
    def account(self) -> Optional[AccountSnap]:
        a = self.mt5.account_info()
        if a is None:
            return None
        return AccountSnap(
            login=a.login,
            currency=a.currency,
            balance=a.balance,
            equity=a.equity,
            margin_mode=a.margin_mode,
            trade_allowed=bool(a.trade_allowed),
        )

    def _get(self, fn):
        items = fn()
        if items is None:
            # Some builds return None (with a "success" code) when there is nothing open.
            code = self.mt5.last_error()[0]
            if code == 1:
                return ()
            return None
        return items

    def positions(self) -> Optional[list]:
        items = self._get(self.mt5.positions_get)
        if items is None:
            return None
        return [
            PositionSnap(
                id=p.identifier,
                ticket=p.ticket,
                symbol=p.symbol,
                type=p.type,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                magic=p.magic,
                comment=p.comment,
            )
            for p in items
        ]

    def orders(self) -> Optional[list]:
        items = self._get(self.mt5.orders_get)
        if items is None:
            return None
        return [
            OrderSnap(
                ticket=o.ticket,
                symbol=o.symbol,
                type=o.type,
                volume=o.volume_current,
                price_open=o.price_open,
                sl=o.sl,
                tp=o.tp,
                magic=o.magic,
                comment=o.comment,
            )
            for o in items
            if o.type in PENDING_TYPES
        ]

    def symbol_info(self, name: str) -> Optional[SymbolSpec]:
        info = self.mt5.symbol_info(name)
        if info is None:
            return None
        if not info.visible:
            self.mt5.symbol_select(name, True)
        return SymbolSpec(
            name=info.name,
            digits=info.digits,
            point=info.point,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            contract_size=info.trade_contract_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            filling_mode=info.filling_mode,
            trade_mode=info.trade_mode,
        )

    def tick(self, name: str) -> Optional[Tick]:
        t = self.mt5.symbol_info_tick(name)
        if t is None:
            self.mt5.symbol_select(name, True)
            t = self.mt5.symbol_info_tick(name)
            if t is None:
                return None
        return Tick(bid=t.bid, ask=t.ask)

    # -- writes -----------------------------------------------------------
    def send(self, request: dict) -> SendResult:
        result = self.mt5.order_send(request)
        if result is None:
            return SendResult(retcode=-1, comment=f"order_send failed: {self.mt5.last_error()}")
        return SendResult(retcode=result.retcode, order=result.order, deal=result.deal, comment=result.comment)
