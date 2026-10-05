"""Reading the master account and packaging it as a snapshot."""

from __future__ import annotations

import time
from typing import Optional

from .config import MasterConfig
from .models import MasterSnapshot


class MasterWatcher:
    def __init__(self, broker, cfg: MasterConfig, clock=time.time):
        self.broker = broker
        self.cfg = cfg
        self.clock = clock
        self.seq = 0
        self._specs: dict = {}

    def snapshot(self) -> Optional[MasterSnapshot]:
        """A complete picture of the master, or None if anything could not be read.

        Returning None (rather than an empty list) matters: an empty snapshot
        would tell every slave to close all of its trades.
        """
        if not self.broker.is_connected():
            return None
        account = self.broker.account()
        positions = self.broker.positions()
        orders = self.broker.orders()
        if account is None or positions is None or orders is None:
            return None
        if self.cfg.only_manual_trades:
            positions = [p for p in positions if p.magic == 0]
            orders = [o for o in orders if o.magic == 0]

        symbols = {}
        for name in {p.symbol for p in positions} | {o.symbol for o in orders}:
            spec = self._specs.get(name) or self.broker.symbol_info(name)
            if spec is not None:
                self._specs[name] = spec
                symbols[name] = spec

        self.seq += 1
        return MasterSnapshot(
            seq=self.seq,
            created=self.clock(),
            account=account,
            positions=list(positions),
            orders=list(orders),
            symbols=symbols,
        )


def fingerprint(snap: MasterSnapshot) -> tuple:
    """What matters for copying; used to decide when to push a new snapshot."""
    return (
        tuple(sorted((p.id, p.symbol, p.type, p.volume, p.sl, p.tp) for p in snap.positions)),
        tuple(sorted((o.ticket, o.symbol, o.type, o.volume, o.price_open, o.sl, o.tp) for o in snap.orders)),
    )
