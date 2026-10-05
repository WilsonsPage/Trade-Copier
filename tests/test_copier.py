import pytest

from trade_copier.config import LotConfig, MasterConfig, RiskConfig, SlaveConfig, SymbolConfig
from trade_copier.constants import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_SELL,
    ORDER_TYPE_SELL_STOP,
    TRADE_ACTION_DEAL,
    TRADE_RETCODE_INVALID_FILL,
    TRADE_RETCODE_INVALID_STOPS,
)
from trade_copier.copier import NEW_TRADE_GRACE_SECONDS, SlaveCopier
from trade_copier.models import AccountSnap, MasterSnapshot, OrderSnap, PositionSnap
from trade_copier.state import SlaveState

from .fakes import Clock, FakeBroker, spec

MASTER_ACCT = AccountSnap(login=9, currency="USD", balance=10_000.0, equity=10_000.0)


def make(slave_kwargs=None, broker=None, state=None, master_cfg=None):
    clock = Clock()
    broker = broker or FakeBroker()
    broker.add_symbol(spec("EURUSD"), 1.10000, 1.10010)
    cfg = SlaveConfig(name="s1", terminal_path="x", magic=42, **(slave_kwargs or {}))
    copier = SlaveCopier(cfg, master_cfg or MasterConfig(terminal_path="m"), broker,
                         state or SlaveState(), clock=clock)
    return copier, broker, clock


def snap(positions=(), orders=(), account=MASTER_ACCT, symbols=None):
    return MasterSnapshot(seq=1, created=0, account=account, positions=list(positions),
                          orders=list(orders), symbols=symbols or {})


def mpos(id=1, symbol="EURUSD", type=ORDER_TYPE_BUY, volume=0.10, price=1.10005, sl=1.09500, tp=1.11000):
    return PositionSnap(id=id, ticket=id, symbol=symbol, type=type, volume=volume, price_open=price, sl=sl, tp=tp)


def test_existing_master_trades_are_ignored_on_first_run():
    copier, broker, _ = make()
    copier.process(snap([mpos(1)]))
    assert broker.pos == {}


def test_copy_existing_on_start():
    copier, broker, _ = make({"copy_existing_on_start": True})
    copier.process(snap([mpos(1)]))
    assert len(broker.pos) == 1


def test_opens_with_multiplier_and_copies_sl_tp():
    copier, broker, _ = make({"lots": LotConfig(mode="multiplier", multiplier=10)})
    copier.process(snap())
    copier.process(snap([mpos(1, volume=0.15)]))
    (p,) = broker.pos.values()
    assert p.volume == 1.5
    assert p.type == ORDER_TYPE_BUY
    assert p.sl == 1.095 and p.tp == 1.11
    assert p.magic == 42 and p.comment == "TC|1"
    assert broker.sent[-1]["price"] == 1.10010  # buys at the ask


def test_balance_ratio_10k_master_to_100k_slave():
    copier, broker, _ = make({"lots": LotConfig(mode="balance_ratio")}, broker=FakeBroker(balance=100_000))
    copier.process(snap())
    copier.process(snap([mpos(1, volume=0.25)]))
    assert next(iter(broker.pos.values())).volume == 2.5


def test_balance_ratio_converts_currency():
    broker = FakeBroker(currency="USD", balance=127_000)
    copier, broker, _ = make({"lots": LotConfig(mode="balance_ratio")}, broker=broker)
    broker.add_symbol(spec("GBPUSD"), 1.26990, 1.27010)
    gbp_master = AccountSnap(login=9, currency="GBP", balance=10_000, equity=10_000)
    copier.process(snap(account=gbp_master))
    copier.process(snap([mpos(1, volume=0.10)], account=gbp_master))
    # 10k GBP = 12.7k USD; 127k/12.7k = 10x
    assert next(iter(broker.pos.values())).volume == 1.0


def test_no_change_means_no_requests():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    n = len(broker.sent)
    copier.process(snap([mpos(1)]))
    copier.process(snap([mpos(1)]))
    assert len(broker.sent) == n


def test_sl_tp_modification_is_copied():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    copier.process(snap([mpos(1, sl=1.10000, tp=1.12000)]))
    p = next(iter(broker.pos.values()))
    assert (p.sl, p.tp) == (1.1, 1.12)


def test_close_on_master_closes_slave():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    assert broker.pos
    copier.process(snap([]))
    assert broker.pos == {}
    assert copier.state.links == {}


def test_partial_close_is_proportional():
    copier, broker, _ = make({"lots": LotConfig(multiplier=10)})
    copier.process(snap())
    copier.process(snap([mpos(1, volume=1.0)]))
    copier.process(snap([mpos(1, volume=0.4)]))
    assert next(iter(broker.pos.values())).volume == pytest.approx(4.0)


def test_reverse_flips_direction_and_swaps_sl_tp():
    copier, broker, _ = make({"reverse": True})
    copier.process(snap())
    copier.process(snap([mpos(1, type=ORDER_TYPE_BUY, sl=1.095, tp=1.11)]))
    p = next(iter(broker.pos.values()))
    assert p.type == ORDER_TYPE_SELL
    assert (p.sl, p.tp) == (1.11, 1.095)
    assert broker.sent[-1]["price"] == 1.10000  # sells at the bid


def test_copy_sl_off_leaves_slave_sl_alone():
    copier, broker, _ = make({"risk": RiskConfig(copy_sl=False)})
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    p = next(iter(broker.pos.values()))
    assert p.sl == 0 and p.tp == 1.11
    copier.process(snap([mpos(1, sl=1.09)]))
    assert next(iter(broker.pos.values())).sl == 0


def test_symbol_mapping_and_unknown_symbol_skipped():
    copier, broker, _ = make({"symbols": SymbolConfig(map={"XAUUSD": "GOLD"}, suffix=".pro")})
    broker.add_symbol(spec("GOLD", digits=2, point=0.01, contract=100), 2000.0, 2000.3)
    broker.add_symbol(spec("EURUSD.pro"), 1.1, 1.1001)
    copier.process(snap())
    copier.process(snap([mpos(1, symbol="XAUUSD", price=2000.1, sl=1990, tp=2020),
                         mpos(2, symbol="EURUSD"), mpos(3, symbol="NOPE")]))
    symbols = sorted(p.symbol for p in broker.pos.values())
    assert symbols == ["EURUSD.pro", "GOLD"]
    assert 3 in copier.state.skipped


def test_include_exclude_filters():
    copier, broker, _ = make({"symbols": SymbolConfig(exclude=["EURUSD"])})
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    assert broker.pos == {}


def test_direction_buy_only():
    copier, broker, _ = make({"direction": "buy_only"})
    copier.process(snap())
    copier.process(snap([mpos(1, type=ORDER_TYPE_SELL, sl=1.11, tp=1.09)]))
    assert broker.pos == {}


def test_price_deviation_guard():
    copier, broker, _ = make({"risk": RiskConfig(max_price_deviation_points=50)})
    copier.process(snap())
    copier.process(snap([mpos(1, price=1.09000)]))  # 1000 points away
    assert broker.pos == {} and 1 in copier.state.skipped


def test_trade_closed_on_slave_is_not_reopened():
    copier, broker, clock = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    broker.pos.clear()  # e.g. slave SL hit or closed by hand
    clock.advance(NEW_TRADE_GRACE_SECONDS + 1)
    copier.process(snap([mpos(1)]))
    copier.process(snap([mpos(1)]))
    assert broker.pos == {}
    assert copier.state.links[1].status == "slave_closed"


def test_failed_open_retries_with_backoff_then_gives_up():
    copier, broker, clock = make({"risk": RiskConfig(max_retries=3)})
    copier.process(snap())
    broker.fail_next = [10004] * 10
    for _ in range(10):
        copier.process(snap([mpos(1)]))
        clock.advance(100)
    deals = [r for r in broker.sent if r["action"] == TRADE_ACTION_DEAL]
    assert len(deals) == 3
    assert 1 in copier.state.skipped


def test_unsupported_filling_mode_falls_back():
    copier, broker, _ = make()
    copier.process(snap())
    broker.fail_next = [TRADE_RETCODE_INVALID_FILL]
    copier.process(snap([mpos(1)]))
    assert len(broker.pos) == 1
    assert broker.sent[-1]["type_filling"] != broker.sent[-2]["type_filling"]


def test_invalid_stops_opens_without_then_retries_sltp():
    copier, broker, clock = make()
    copier.process(snap())
    broker.fail_next = [TRADE_RETCODE_INVALID_STOPS]
    copier.process(snap([mpos(1)]))
    p = next(iter(broker.pos.values()))
    assert p.sl == 0
    copier.process(snap([mpos(1)]))
    p = next(iter(broker.pos.values()))
    assert p.sl == 1.095


def test_below_min_lot_skipped_unless_round_up():
    copier, broker, _ = make({"lots": LotConfig(multiplier=0.01)})
    copier.process(snap())
    copier.process(snap([mpos(1, volume=0.10)]))
    assert broker.pos == {}
    copier2, broker2, _ = make({"lots": LotConfig(multiplier=0.01, round_up_to_min_lot=True)})
    copier2.process(snap())
    copier2.process(snap([mpos(1, volume=0.10)]))
    assert next(iter(broker2.pos.values())).volume == 0.01


def test_max_drawdown_blocks_new_trades_but_still_closes():
    broker = FakeBroker(balance=100_000)
    copier, broker, _ = make({"risk": RiskConfig(max_drawdown_percent=5)}, broker=broker)
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    broker.acct = AccountSnap(login=1, currency="USD", balance=100_000, equity=94_000)
    copier.process(snap([mpos(1), mpos(2)]))
    assert len(broker.pos) == 1
    copier.process(snap([]))
    assert broker.pos == {}


def test_trades_are_tagged_and_other_trades_untouched():
    copier, broker, _ = make()
    broker.pos[5] = PositionSnap(id=5, ticket=5, symbol="EURUSD", type=0, volume=1, price_open=1.1, magic=0)
    copier.process(snap())
    copier.process(snap([]))
    assert 5 in broker.pos  # manual trade on the slave left alone


# -- pending orders -----------------------------------------------------

def morder(ticket=7, type=ORDER_TYPE_BUY_LIMIT, price=1.09000, volume=0.1, sl=1.085, tp=1.1):
    return OrderSnap(ticket=ticket, symbol="EURUSD", type=type, volume=volume, price_open=price, sl=sl, tp=tp)


def test_pending_order_lifecycle():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap(orders=[morder()]))
    (o,) = broker.ords.values()
    assert o.type == ORDER_TYPE_BUY_LIMIT and o.price_open == 1.09

    copier.process(snap(orders=[morder(price=1.08900, sl=1.084)]))
    (o,) = broker.ords.values()
    assert (o.price_open, o.sl) == (1.089, 1.084)

    copier.process(snap())  # master deleted it
    assert broker.ords == {}


def test_pending_fills_on_both_sides():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap(orders=[morder()]))
    (slave_ticket,) = broker.ords
    broker.fill_order(slave_ticket)
    master_position = mpos(7, price=1.09, sl=1.085, tp=1.1)
    copier.process(snap([master_position]))
    assert list(broker.pos) == [slave_ticket]
    copier.process(snap())
    assert broker.pos == {}


def test_master_pending_fills_first_converts_to_market():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap(orders=[morder()]))
    copier.process(snap([mpos(7, price=1.09)]))
    assert broker.ords == {}
    (p,) = broker.pos.values()
    assert p.volume == 0.1 and p.comment == "TC|7"


def test_reverse_pending_type():
    copier, broker, _ = make({"reverse": True})
    copier.process(snap())
    copier.process(snap(orders=[morder(type=ORDER_TYPE_BUY_LIMIT)]))
    assert next(iter(broker.ords.values())).type == ORDER_TYPE_SELL_STOP


def test_pending_orders_can_be_disabled():
    copier, broker, _ = make({"copy_pending_orders": False})
    copier.process(snap())
    copier.process(snap(orders=[morder()]))
    assert broker.ords == {}


# -- restarts / state ------------------------------------------------------

def test_state_survives_restart(tmp_path):
    path = tmp_path / "s1.json"
    copier, broker, _ = make(state=SlaveState.load(path))
    copier.process(snap())
    copier.process(snap([mpos(1)]))

    # restart: new copier, same broker and state file; master closed while we were down
    copier2 = SlaveCopier(copier.cfg, MasterConfig(terminal_path="m"), broker, SlaveState.load(path), clock=Clock())
    copier2.process(snap([]))
    assert broker.pos == {}


def test_lost_state_file_recovers_from_comments():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    copier2 = SlaveCopier(copier.cfg, MasterConfig(terminal_path="m"), broker, SlaveState(), clock=Clock())
    copier2.process(snap([mpos(1)]))
    assert len(broker.pos) == 1  # not duplicated
    copier2.process(snap([]))
    assert broker.pos == {}


def test_trade_opened_while_copier_was_down_is_copied(tmp_path):
    path = tmp_path / "s1.json"
    copier, broker, _ = make(state=SlaveState.load(path))
    copier.process(snap())
    copier2 = SlaveCopier(copier.cfg, MasterConfig(terminal_path="m"), broker, SlaveState.load(path), clock=Clock())
    copier2.process(snap([mpos(1)]))
    assert len(broker.pos) == 1


def test_unreadable_slave_does_nothing():
    copier, broker, _ = make()
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    broker.positions = lambda: None
    copier.process(snap([]))
    assert len(broker.pos) == 1


def test_dry_run_sends_nothing():
    copier, broker, _ = make({"dry_run": True})
    copier.process(snap())
    copier.process(snap([mpos(1)]))
    assert broker.sent == [] and broker.pos == {}
