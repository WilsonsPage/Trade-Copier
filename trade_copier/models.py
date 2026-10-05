"""Plain data snapshots passed between the master watcher and slave copiers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountSnap:
    login: int
    currency: str
    balance: float
    equity: float
    margin_mode: int = 2
    trade_allowed: bool = True


@dataclass(frozen=True)
class PositionSnap:
    id: int  # position identifier (stable for the life of the position)
    ticket: int
    symbol: str
    type: int  # ORDER_TYPE_BUY or ORDER_TYPE_SELL
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""


@dataclass(frozen=True)
class OrderSnap:
    ticket: int
    symbol: str
    type: int  # one of the pending order types
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # value of one tick for 1 lot, in the account currency
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    filling_mode: int = 1
    trade_mode: int = 4


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float


@dataclass(frozen=True)
class SendResult:
    retcode: int
    order: int = 0
    deal: int = 0
    comment: str = ""


@dataclass
class MasterSnapshot:
    seq: int
    created: float  # time.time() when the snapshot was taken
    account: AccountSnap
    positions: list = field(default_factory=list)  # list[PositionSnap]
    orders: list = field(default_factory=list)  # list[OrderSnap] (pending only)
    symbols: dict = field(default_factory=dict)  # master symbol name -> SymbolSpec
