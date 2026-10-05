import queue

from trade_copier.config import MasterConfig
from trade_copier.master import MasterWatcher, fingerprint
from trade_copier.models import PositionSnap
from trade_copier.runner import _publish

from .fakes import FakeBroker, spec


def test_snapshot_none_when_master_unreadable():
    b = FakeBroker()
    w = MasterWatcher(b, MasterConfig(terminal_path="m"))
    assert w.snapshot() is not None
    b.positions = lambda: None
    assert w.snapshot() is None  # never an "empty" snapshot that would close everything
    b.connected = False
    assert w.snapshot() is None


def test_only_manual_trades_and_symbol_specs():
    b = FakeBroker()
    b.add_symbol(spec("EURUSD"), 1.1, 1.1)
    b.pos[1] = PositionSnap(id=1, ticket=1, symbol="EURUSD", type=0, volume=1, price_open=1.1, magic=0)
    b.pos[2] = PositionSnap(id=2, ticket=2, symbol="EURUSD", type=0, volume=1, price_open=1.1, magic=99)
    s = MasterWatcher(b, MasterConfig(terminal_path="m", only_manual_trades=True)).snapshot()
    assert [p.id for p in s.positions] == [1]
    assert "EURUSD" in s.symbols


def test_fingerprint_ignores_account_moves_but_sees_sl_change():
    b = FakeBroker()
    b.pos[1] = PositionSnap(id=1, ticket=1, symbol="EURUSD", type=0, volume=1, price_open=1.1, sl=1.0)
    w = MasterWatcher(b, MasterConfig(terminal_path="m"))
    a = fingerprint(w.snapshot())
    assert fingerprint(w.snapshot()) == a
    b.pos[1] = PositionSnap(id=1, ticket=1, symbol="EURUSD", type=0, volume=1, price_open=1.1, sl=1.05)
    assert fingerprint(w.snapshot()) != a


def test_publish_keeps_latest_when_full():
    q = queue.Queue(maxsize=1)
    _publish(q, "old")
    _publish(q, "new")
    assert q.get_nowait() == "new"
