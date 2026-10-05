"""The heart of the copier: make a slave account mirror the master's trades.

Every time a fresh master snapshot arrives, ``SlaveCopier.process`` compares
what the master has open with what this slave has open and closes the gap:

* new master position / pending order  -> open it on the slave (sized per config)
* master SL/TP or pending price changed -> modify the slave trade
* master partially closed              -> close the same proportion on the slave
* master trade gone                    -> close / delete it on the slave
* master pending order filled first    -> optionally convert the slave's to market

Because it works from full snapshots rather than one-off events, a restart
or a dropped connection never leaves trades orphaned: the next snapshot
reconciles everything.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .config import MasterConfig, SlaveConfig
from .constants import (
    BUY_TYPES,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TIME_GTC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    PENDING_TYPES,
    REVERSE_TYPE,
    SUCCESS_RETCODES,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_MODE_CLOSEONLY,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_LONGONLY,
    SYMBOL_TRADE_MODE_SHORTONLY,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP,
    TRADE_RETCODE_INVALID_FILL,
    TRADE_RETCODE_INVALID_STOPS,
    TYPE_NAMES,
)
from .fx import conversion_rate
from .models import MasterSnapshot, SendResult, SymbolSpec
from .sizing import SizingInput, compute_volume, round_volume
from .state import Link, SlaveState, make_tag, parse_tag
from .symbols import SymbolMapper

# How long a freshly opened slave trade may be missing from the terminal's
# lists before we conclude it was closed (orders can take a moment to show).
NEW_TRADE_GRACE_SECONDS = 10.0
SPEC_CACHE_SECONDS = 60.0


class Backoff:
    """Per-key retry timer: 1s, 2s, 4s ... capped, with an attempt counter."""

    def __init__(self, clock: Callable[[], float], base: float = 1.0, cap: float = 60.0):
        self.clock = clock
        self.base = base
        self.cap = cap
        self._attempts: dict = {}
        self._next: dict = {}

    def ready(self, key) -> bool:
        return self.clock() >= self._next.get(key, 0.0)

    def attempts(self, key) -> int:
        return self._attempts.get(key, 0)

    def failed(self, key) -> int:
        n = self._attempts.get(key, 0) + 1
        self._attempts[key] = n
        self._next[key] = self.clock() + min(self.cap, self.base * 2 ** (n - 1))
        return n

    def clear(self, key) -> None:
        self._attempts.pop(key, None)
        self._next.pop(key, None)


class SlaveCopier:
    def __init__(
        self,
        cfg: SlaveConfig,
        master_cfg: MasterConfig,
        broker,
        state: SlaveState,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self.cfg = cfg
        self.broker = broker
        self.state = state
        self.clock = clock
        self.log = logger or logging.getLogger(f"trade_copier.{cfg.name}")
        self.mapper = SymbolMapper(cfg.symbols, master_cfg.symbol_prefix, master_cfg.symbol_suffix)
        self.backoff = Backoff(clock)
        self._specs: dict = {}
        self._warned: set = set()
        self._dd_blocked = False

    # ------------------------------------------------------------------
    # main entry point
    # ------------------------------------------------------------------
    def process(self, snap: MasterSnapshot) -> None:
        acct = self.broker.account()
        positions = self.broker.positions()
        orders = self.broker.orders()
        if acct is None or positions is None or orders is None:
            self._warn_once("read-fail", "could not read account/positions from the slave terminal; skipping")
            return
        self._warned.discard("read-fail")

        self.snap = snap
        self.acct = acct
        own_pos = [p for p in positions if p.magic == self.cfg.magic]
        own_ord = [o for o in orders if o.magic == self.cfg.magic]
        self.pos_by_id = {p.id: p for p in own_pos}
        self.ord_by_id = {o.ticket: o for o in own_ord}
        self.by_tag: dict = {}
        for p in own_pos:
            mid = parse_tag(p.comment)
            if mid is not None:
                self.by_tag[mid] = ("position", p)
        for o in own_ord:
            mid = parse_tag(o.comment)
            if mid is not None:
                self.by_tag.setdefault(mid, ("order", o))
        self.open_count = len(own_pos) + len(own_ord)

        master_pos = {p.id: p for p in snap.positions}
        master_ord = {o.ticket: o for o in snap.orders if o.type in PENDING_TYPES}
        master_ids = set(master_pos) | set(master_ord)

        self._recover_links(master_pos, master_ord)
        if not self.state.seeded:
            if not self.cfg.copy_existing_on_start:
                existing = master_ids - set(self.state.links)
                self.state.known |= existing
                if existing:
                    self.log.info(
                        "first run: ignoring %d trade(s) already open on the master "
                        "(set copy_existing_on_start: true to copy them)",
                        len(existing),
                    )
            self.state.seeded = True
            self.state.dirty = True

        self._update_drawdown_guard()

        # 1) master trade gone -> close/delete on slave
        for link in list(self.state.links.values()):
            if link.master_id not in master_ids:
                self._handle_master_gone(link)

        # 2) sync / open everything the master has
        for mid, mp in master_pos.items():
            try:
                self._sync_position(mid, mp)
            except Exception:  # keep one bad trade from stopping the rest
                self.log.exception("error while syncing master position #%s", mid)
        for mid, mo in master_ord.items():
            try:
                self._sync_order(mid, mo)
            except Exception:
                self.log.exception("error while syncing master order #%s", mid)

        # 3) forget ids that no longer exist on the master
        for bucket in (self.state.known, self.state.skipped):
            stale = bucket - master_ids
            if stale:
                bucket -= stale
                self.state.dirty = True

        self.state.save()

    # ------------------------------------------------------------------
    # bookkeeping helpers
    # ------------------------------------------------------------------
    def _warn_once(self, key, msg, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.log.warning(msg, *args)

    def _spec(self, symbol: str) -> Optional[SymbolSpec]:
        cached = self._specs.get(symbol)
        now = self.clock()
        if cached and now - cached[0] < SPEC_CACHE_SECONDS:
            return cached[1]
        spec = self.broker.symbol_info(symbol)
        if spec is not None:
            self._specs[symbol] = (now, spec)
        return spec

    def _find(self, link: Link):
        p = self.pos_by_id.get(link.slave_id)
        if p is not None:
            return "position", p
        o = self.ord_by_id.get(link.slave_id)
        if o is not None:
            return "order", o
        return self.by_tag.get(link.master_id)

    def _recover_links(self, master_pos, master_ord) -> None:
        """Rebuild links from trade comments if the state file was lost."""
        for mid, (kind, item) in self.by_tag.items():
            if mid in self.state.links:
                continue
            master_item = master_pos.get(mid) or master_ord.get(mid)
            slave_id = item.id if kind == "position" else item.ticket
            self.state.links[mid] = Link(
                master_id=mid,
                slave_id=slave_id,
                kind=kind,
                symbol=item.symbol,
                master_volume=master_item.volume if master_item else item.volume,
                slave_volume=item.volume,
                created=0.0,
            )
            self.state.dirty = True
            self.log.info("recovered link master #%s -> slave #%s from trade comment", mid, slave_id)

    def _update_drawdown_guard(self) -> None:
        if self.acct.equity > self.state.equity_peak:
            self.state.equity_peak = self.acct.equity
            self.state.dirty = True
        limit = self.cfg.risk.max_drawdown_percent
        if not limit or self.state.equity_peak <= 0:
            self._dd_blocked = False
            return
        dd = (1 - self.acct.equity / self.state.equity_peak) * 100
        blocked = dd >= limit
        if blocked and not self._dd_blocked:
            self.log.warning(
                "equity drawdown %.1f%% reached the %.1f%% limit: new trades will NOT be copied "
                "(existing trades are still managed). Delete equity_peak in the state file to reset.",
                dd,
                limit,
            )
        self._dd_blocked = blocked

    def _skip(self, mid: int, reason: str) -> None:
        self.state.skipped.add(mid)
        self.state.dirty = True
        self.log.warning("not copying master #%s: %s", mid, reason)

    # ------------------------------------------------------------------
    # prices, SL/TP, request plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _round_price(price: float, spec: SymbolSpec) -> float:
        if not price:
            return 0.0
        if spec.tick_size and spec.tick_size > 0:
            price = round(price / spec.tick_size) * spec.tick_size
        return round(price, spec.digits)

    @staticmethod
    def _same_price(a: float, b: float, spec: SymbolSpec) -> bool:
        return abs((a or 0.0) - (b or 0.0)) < (spec.point or 1e-9) / 2

    def _wanted_sltp(self, m_sl: float, m_tp: float):
        """SL/TP the slave should have. None means "leave whatever the slave has"."""
        if self.cfg.reverse:
            m_sl, m_tp = m_tp, m_sl
        sl = m_sl if self.cfg.risk.copy_sl else None
        tp = m_tp if self.cfg.risk.copy_tp else None
        return sl, tp

    def _slave_type(self, master_type: int) -> int:
        return REVERSE_TYPE[master_type] if self.cfg.reverse else master_type

    @staticmethod
    def _fillings(spec: SymbolSpec) -> list:
        order = []
        if spec.filling_mode & SYMBOL_FILLING_FOK:
            order.append(ORDER_FILLING_FOK)
        if spec.filling_mode & SYMBOL_FILLING_IOC:
            order.append(ORDER_FILLING_IOC)
        for f in (ORDER_FILLING_RETURN, ORDER_FILLING_FOK, ORDER_FILLING_IOC):
            if f not in order:
                order.append(f)
        return order

    def _send(self, request: dict, spec: Optional[SymbolSpec] = None) -> SendResult:
        if self.cfg.dry_run:
            self.log.info("[dry run] would send %s", request)
            return SendResult(retcode=10009, order=-1, comment="dry run")
        if spec is None or request["action"] not in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
            return self.broker.send(request)
        result = SendResult(retcode=-1)
        for filling in self._fillings(spec):
            result = self.broker.send({**request, "type_filling": filling})
            if result.retcode != TRADE_RETCODE_INVALID_FILL:
                break
        return result

    @staticmethod
    def _ok(result: SendResult) -> bool:
        return result.retcode in SUCCESS_RETCODES

    # ------------------------------------------------------------------
    # master trade disappeared
    # ------------------------------------------------------------------
    def _handle_master_gone(self, link: Link) -> None:
        found = self._find(link)
        if found is None:
            if link.status == "open" and self.clock() - link.created < NEW_TRADE_GRACE_SECONDS:
                return
            del self.state.links[link.master_id]
            self.state.dirty = True
            return
        kind, item = found
        key = ("close", link.master_id)
        if not self.backoff.ready(key):
            return
        if kind == "position":
            result = self._close_position(item, item.volume, link.master_id)
            what = f"closed slave position #{item.ticket} ({item.symbol} {item.volume})"
        else:
            result = self._send({"action": TRADE_ACTION_REMOVE, "order": item.ticket})
            what = f"deleted slave pending order #{item.ticket} ({item.symbol})"
        if self._ok(result):
            self.backoff.clear(key)
            self.log.info("master #%s closed -> %s", link.master_id, what)
            # If anything is left over (partial fill), the comment tag re-links it next pass.
            del self.state.links[link.master_id]
            self.state.dirty = True
        else:
            n = self.backoff.failed(key)  # closes are retried forever
            self.log.error(
                "failed to close slave trade for master #%s (attempt %d): %s %s",
                link.master_id, n, result.retcode, result.comment,
            )

    def _close_position(self, pos, volume: float, master_id: int) -> SendResult:
        spec = self._spec(pos.symbol)
        tick = self.broker.tick(pos.symbol)
        if spec is None or tick is None:
            return SendResult(retcode=-1, comment=f"no price/spec for {pos.symbol}")
        is_buy = pos.type == ORDER_TYPE_BUY
        request = {
            "action": TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "volume": volume,
            "type": ORDER_TYPE_SELL if is_buy else ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.cfg.risk.slippage_points,
            "magic": self.cfg.magic,
            "comment": make_tag(master_id),
            "type_time": ORDER_TIME_GTC,
        }
        return self._send(request, spec)

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    def _sync_position(self, mid: int, mp) -> None:
        link = self.state.links.get(mid)
        if link is None:
            if mid in self.state.known or mid in self.state.skipped:
                return
            self._open(mid, mp, pending=False)
            return
        if link.status != "open":
            return
        found = self._find(link)
        if found is None:
            if self.clock() - link.created < NEW_TRADE_GRACE_SECONDS:
                return
            link.status = "slave_closed"
            self.state.dirty = True
            self.log.info(
                "slave trade for master #%s is gone (closed on the slave); it will not be reopened", mid
            )
            return
        kind, item = found

        if kind == "order":
            # Master's pending order filled but the slave's hasn't yet.
            if self.cfg.pending_fill_to_market:
                self._convert_to_market(link, item, mp)
            return

        if link.slave_id != item.id or link.kind != "position":
            link.slave_id, link.kind = item.id, "position"
            self.state.dirty = True

        spec = self._spec(item.symbol)
        if spec is None:
            return
        self._sync_volume(link, item, mp, spec)
        want_sl, want_tp = self._wanted_sltp(mp.sl, mp.tp)
        self._sync_sltp(mid, item, want_sl, want_tp, spec)

    def _sync_volume(self, link: Link, item, mp, spec: SymbolSpec) -> None:
        if link.master_volume <= 0:
            return
        target = round_volume(link.slave_volume * mp.volume / link.master_volume, spec, "nearest")
        target = max(target, spec.volume_min)
        excess = round_volume(item.volume - target, spec, "nearest")
        if excess >= spec.volume_step - 1e-9:
            key = ("partial", link.master_id, round(mp.volume, 8))
            if not self.backoff.ready(key):
                return
            result = self._close_position(item, excess, link.master_id)
            if self._ok(result):
                self.backoff.clear(key)
                self.log.info(
                    "master #%s partially closed to %s -> closed %s of slave #%s (now %s)",
                    link.master_id, mp.volume, excess, item.ticket, target,
                )
            else:
                n = self.backoff.failed(key)
                self.log.error("partial close failed for master #%s (attempt %d): %s %s",
                               link.master_id, n, result.retcode, result.comment)
        elif target - item.volume >= spec.volume_step - 1e-9:
            self._warn_once(
                ("grow", link.master_id),
                "master #%s volume increased; adding to an existing position is not copied",
                link.master_id,
            )

    def _sync_sltp(self, mid: int, item, want_sl, want_tp, spec: SymbolSpec) -> None:
        new_sl = item.sl if want_sl is None else self._round_price(want_sl, spec)
        new_tp = item.tp if want_tp is None else self._round_price(want_tp, spec)
        if self._same_price(new_sl, item.sl, spec) and self._same_price(new_tp, item.tp, spec):
            return
        key = ("sltp", mid, new_sl, new_tp)
        if not self.backoff.ready(key) or self.backoff.attempts(key) >= self.cfg.risk.max_retries:
            return
        result = self._send({
            "action": TRADE_ACTION_SLTP,
            "position": item.ticket,
            "symbol": item.symbol,
            "sl": new_sl,
            "tp": new_tp,
            "magic": self.cfg.magic,
        })
        if self._ok(result):
            self.backoff.clear(key)
            self.log.info("master #%s SL/TP -> slave #%s SL=%s TP=%s", mid, item.ticket, new_sl, new_tp)
        else:
            n = self.backoff.failed(key)
            self.log.error("SL/TP update failed for master #%s (attempt %d/%d): %s %s",
                           mid, n, self.cfg.risk.max_retries, result.retcode, result.comment)

    def _convert_to_market(self, link: Link, slave_order, mp) -> None:
        key = ("convert", link.master_id)
        if not self.backoff.ready(key):
            return
        result = self._send({"action": TRADE_ACTION_REMOVE, "order": slave_order.ticket})
        if not self._ok(result):
            self.backoff.failed(key)
            self.log.error("could not delete slave pending #%s to convert to market: %s %s",
                           slave_order.ticket, result.retcode, result.comment)
            return
        self.log.info("master #%s pending order filled -> replacing slave pending #%s with a market order",
                      link.master_id, slave_order.ticket)
        del self.state.links[link.master_id]
        self.state.dirty = True
        self._open(link.master_id, mp, pending=False, volume_override=link.slave_volume)

    # ------------------------------------------------------------------
    # pending orders
    # ------------------------------------------------------------------
    def _sync_order(self, mid: int, mo) -> None:
        link = self.state.links.get(mid)
        if link is None:
            if not self.cfg.copy_pending_orders:
                return
            if mid in self.state.known or mid in self.state.skipped:
                return
            self._open(mid, mo, pending=True)
            return
        if link.status != "open":
            return
        found = self._find(link)
        if found is None:
            if self.clock() - link.created < NEW_TRADE_GRACE_SECONDS:
                return
            link.status = "slave_closed"
            self.state.dirty = True
            self.log.info("slave pending order for master #%s is gone; it will not be re-placed", mid)
            return
        kind, item = found
        spec = self._spec(item.symbol)
        if spec is None:
            return
        want_sl, want_tp = self._wanted_sltp(mo.sl, mo.tp)
        if kind == "position":
            # The slave's order filled before the master's: keep it, track SL/TP.
            self._sync_sltp(mid, item, want_sl, want_tp, spec)
            return

        new_price = self._round_price(mo.price_open, spec)
        new_sl = item.sl if want_sl is None else self._round_price(want_sl, spec)
        new_tp = item.tp if want_tp is None else self._round_price(want_tp, spec)
        if (self._same_price(new_price, item.price_open, spec)
                and self._same_price(new_sl, item.sl, spec)
                and self._same_price(new_tp, item.tp, spec)):
            return
        key = ("modify", mid, new_price, new_sl, new_tp)
        if not self.backoff.ready(key) or self.backoff.attempts(key) >= self.cfg.risk.max_retries:
            return
        result = self._send({
            "action": TRADE_ACTION_MODIFY,
            "order": item.ticket,
            "symbol": item.symbol,
            "price": new_price,
            "sl": new_sl,
            "tp": new_tp,
            "type_time": ORDER_TIME_GTC,
        })
        if self._ok(result):
            self.backoff.clear(key)
            self.log.info("master #%s order modified -> slave #%s price=%s SL=%s TP=%s",
                          mid, item.ticket, new_price, new_sl, new_tp)
        else:
            n = self.backoff.failed(key)
            self.log.error("modify failed for master #%s (attempt %d/%d): %s %s",
                           mid, n, self.cfg.risk.max_retries, result.retcode, result.comment)

    # ------------------------------------------------------------------
    # opening new trades
    # ------------------------------------------------------------------
    def _open(self, mid: int, m, pending: bool, volume_override: Optional[float] = None) -> None:
        key = ("open", mid)
        if not self.backoff.ready(key):
            return
        cfg = self.cfg
        if volume_override is None:
            # These checks only apply to brand-new trades, not to a pending->market conversion.
            if not self.mapper.allowed(m.symbol):
                return self._skip(mid, f"{m.symbol} is filtered out by symbols.include/exclude")
            is_buy = self._slave_type(m.type) in BUY_TYPES
            if (cfg.direction == "buy_only" and not is_buy) or (cfg.direction == "sell_only" and is_buy):
                return self._skip(mid, f"direction is {cfg.direction}")
            if cfg.risk.max_open_positions and self.open_count >= cfg.risk.max_open_positions:
                return self._skip(mid, f"max_open_positions ({cfg.risk.max_open_positions}) reached")
            if self._dd_blocked:
                return self._skip(mid, "max_drawdown_percent reached")

        symbol = self.mapper.to_slave(m.symbol)
        spec = self._spec(symbol)
        if spec is None:
            return self._skip(mid, f"symbol {symbol} not found on the slave broker (add it to symbols.map)")
        slave_type = self._slave_type(m.type)
        is_buy = slave_type in BUY_TYPES
        if spec.trade_mode in (SYMBOL_TRADE_MODE_DISABLED, SYMBOL_TRADE_MODE_CLOSEONLY) or \
                (spec.trade_mode == SYMBOL_TRADE_MODE_LONGONLY and not is_buy) or \
                (spec.trade_mode == SYMBOL_TRADE_MODE_SHORTONLY and is_buy):
            return self._skip(mid, f"{symbol} does not allow this trade on the slave (trade mode {spec.trade_mode})")

        tick = self.broker.tick(symbol)
        if tick is None or tick.bid <= 0:
            return self._open_failed(mid, SendResult(retcode=-1, comment=f"no price for {symbol}"))

        if pending:
            price = self._round_price(m.price_open, spec)
        else:
            price = tick.ask if is_buy else tick.bid
            max_dev = cfg.risk.max_price_deviation_points
            if max_dev and spec.point > 0 and volume_override is None:
                dev = abs(price - m.price_open) / spec.point
                if dev > max_dev:
                    return self._skip(
                        mid, f"price moved {dev:.0f} points from the master entry (limit {max_dev})"
                    )

        want_sl, want_tp = self._wanted_sltp(m.sl, m.tp)
        sl = self._round_price(want_sl or 0.0, spec)
        tp = self._round_price(want_tp or 0.0, spec)

        if volume_override is not None:
            volume, reason = volume_override, None
        else:
            fx = None
            if cfg.lots.mode in ("balance_ratio", "equity_ratio"):
                fx = conversion_rate(
                    self.snap.account.currency, self.acct.currency,
                    self.broker.tick, self.mapper.fx_symbol, cfg.fx_rates,
                )
            volume, reason = compute_volume(cfg.lots, SizingInput(
                master_volume=m.volume,
                master_account=self.snap.account,
                slave_account=self.acct,
                slave_spec=spec,
                master_spec=self.snap.symbols.get(m.symbol),
                fx_master_to_slave=fx,
                entry_price=m.price_open,
                sl_price=sl,
            ))
        if volume is None:
            return self._skip(mid, reason)

        request = {
            "action": TRADE_ACTION_PENDING if pending else TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": slave_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": cfg.risk.slippage_points,
            "magic": cfg.magic,
            "comment": make_tag(mid),
            "type_time": ORDER_TIME_GTC,
        }
        result = self._send(request, spec)
        if result.retcode == TRADE_RETCODE_INVALID_STOPS and (sl or tp):
            # Stops too close for this broker: open without them; the SL/TP sync
            # loop will keep trying to set them afterwards.
            self.log.warning("master #%s: slave rejected SL/TP as invalid, opening without them", mid)
            result = self._send({**request, "sl": 0.0, "tp": 0.0}, spec)

        if not self._ok(result):
            return self._open_failed(mid, result)

        self.backoff.clear(key)
        self.state.links[mid] = Link(
            master_id=mid,
            slave_id=result.order,
            kind="order" if pending else "position",
            symbol=symbol,
            master_volume=m.volume,
            slave_volume=volume,
            created=self.clock(),
        )
        self.state.dirty = True
        self.open_count += 1
        self.log.info(
            "copied master #%s %s %s %s @ %s -> slave #%s %s %s %s @ %s SL=%s TP=%s",
            mid, TYPE_NAMES.get(m.type, m.type), m.volume, m.symbol, m.price_open,
            result.order, TYPE_NAMES.get(slave_type, slave_type), volume, symbol, price, sl, tp,
        )

    def _open_failed(self, mid: int, result: SendResult) -> None:
        n = self.backoff.failed(("open", mid))
        self.log.error("open failed for master #%s (attempt %d/%d): %s %s",
                       mid, n, self.cfg.risk.max_retries, result.retcode, result.comment)
        if n >= self.cfg.risk.max_retries:
            self.backoff.clear(("open", mid))
            self._skip(mid, f"gave up after {n} attempts")


__all__ = ["SlaveCopier", "Backoff"]
