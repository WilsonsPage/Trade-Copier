"""An in-memory stand-in for an MT5 terminal, used by the tests."""

from __future__ import annotations

from dataclasses import replace

from trade_copier.constants import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_PLACED,
)
from trade_copier.models import AccountSnap, OrderSnap, PositionSnap, SendResult, SymbolSpec, Tick


def spec(name="EURUSD", digits=5, point=0.00001, tick_value=1.0, contract=100000.0,
         vmin=0.01, vmax=100.0, step=0.01, filling=1, trade_mode=4):
    return SymbolSpec(name=name, digits=digits, point=point, tick_size=point, tick_value=tick_value,
                      contract_size=contract, volume_min=vmin, volume_max=vmax, volume_step=step,
                      filling_mode=filling, trade_mode=trade_mode)


class FakeBroker:
    def __init__(self, currency="USD", balance=100_000.0, equity=None):
        self.acct = AccountSnap(login=1, currency=currency, balance=balance,
                                equity=balance if equity is None else equity)
        self.specs: dict = {}
        self.ticks: dict = {}
        self.pos: dict = {}  # id -> PositionSnap
        self.ords: dict = {}  # ticket -> OrderSnap
        self.sent: list = []
        self.next_ticket = 1000
        self.fail_next: list = []  # retcodes to return for the next sends
        self.connected = True

    # setup helpers
    def add_symbol(self, s: SymbolSpec, bid: float, ask: float):
        self.specs[s.name] = s
        self.ticks[s.name] = Tick(bid, ask)

    def fill_order(self, ticket: int):
        o = self.ords.pop(ticket)
        typ = ORDER_TYPE_BUY if o.type in (2, 4) else ORDER_TYPE_SELL
        self.pos[ticket] = PositionSnap(id=ticket, ticket=ticket, symbol=o.symbol, type=typ, volume=o.volume,
                                        price_open=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic,
                                        comment=o.comment)

    # Broker interface
    def connect(self): pass
    def shutdown(self): pass
    def is_connected(self): return self.connected
    def account(self): return self.acct
    def positions(self): return list(self.pos.values())
    def orders(self): return list(self.ords.values())
    def symbol_info(self, name): return self.specs.get(name)
    def tick(self, name): return self.ticks.get(name)

    def send(self, r: dict) -> SendResult:
        self.sent.append(dict(r))
        if self.fail_next:
            return SendResult(retcode=self.fail_next.pop(0), comment="forced failure")
        a = r["action"]
        if a == TRADE_ACTION_DEAL and "position" in r:
            p = self.pos[r["position"]]
            left = round(p.volume - r["volume"], 8)
            if left <= 0:
                del self.pos[p.id]
            else:
                self.pos[p.id] = replace(p, volume=left)
            return SendResult(retcode=TRADE_RETCODE_DONE)
        if a == TRADE_ACTION_DEAL:
            t = self._ticket()
            self.pos[t] = PositionSnap(id=t, ticket=t, symbol=r["symbol"], type=r["type"], volume=r["volume"],
                                       price_open=r["price"], sl=r["sl"], tp=r["tp"], magic=r["magic"],
                                       comment=r["comment"])
            return SendResult(retcode=TRADE_RETCODE_DONE, order=t, deal=t)
        if a == TRADE_ACTION_PENDING:
            t = self._ticket()
            self.ords[t] = OrderSnap(ticket=t, symbol=r["symbol"], type=r["type"], volume=r["volume"],
                                     price_open=r["price"], sl=r["sl"], tp=r["tp"], magic=r["magic"],
                                     comment=r["comment"])
            return SendResult(retcode=TRADE_RETCODE_PLACED, order=t)
        if a == TRADE_ACTION_SLTP:
            p = self.pos[r["position"]]
            self.pos[p.id] = replace(p, sl=r["sl"], tp=r["tp"])
            return SendResult(retcode=TRADE_RETCODE_DONE)
        if a == TRADE_ACTION_MODIFY:
            o = self.ords[r["order"]]
            self.ords[o.ticket] = replace(o, price_open=r["price"], sl=r["sl"], tp=r["tp"])
            return SendResult(retcode=TRADE_RETCODE_DONE)
        if a == TRADE_ACTION_REMOVE:
            del self.ords[r["order"]]
            return SendResult(retcode=TRADE_RETCODE_DONE)
        raise AssertionError(f"unexpected request {r}")

    def _ticket(self):
        self.next_ticket += 1
        return self.next_ticket


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s
