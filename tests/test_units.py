import os

import pytest

from trade_copier.config import ConfigError, LotConfig, SymbolConfig, parse_config
from trade_copier.fx import conversion_rate
from trade_copier.models import AccountSnap, Tick
from trade_copier.sizing import SizingInput, compute_volume, round_volume
from trade_copier.symbols import SymbolMapper

from .fakes import spec

USD10K = AccountSnap(login=1, currency="USD", balance=10_000, equity=9_000)
USD100K = AccountSnap(login=2, currency="USD", balance=100_000, equity=100_000)


def size(cfg, **kw):
    base = dict(master_volume=0.1, master_account=USD10K, slave_account=USD100K, slave_spec=spec(),
                fx_master_to_slave=1.0)
    base.update(kw)
    return compute_volume(cfg, SizingInput(**base))


def test_round_volume():
    s = spec(step=0.01)
    assert round_volume(0.299999999, s) == 0.3
    assert round_volume(0.237, s) == 0.23
    assert round_volume(0.237, s, "nearest") == 0.24
    assert round_volume(3.7, spec(step=1, vmin=1)) == 3


def test_multiplier():
    assert size(LotConfig(mode="multiplier", multiplier=10)) == (1.0, None)


def test_balance_and_equity_ratio():
    assert size(LotConfig(mode="balance_ratio"))[0] == 1.0
    assert size(LotConfig(mode="equity_ratio"))[0] == pytest.approx(1.11)
    assert size(LotConfig(mode="balance_ratio"), fx_master_to_slave=None)[0] is None


def test_fixed_and_caps():
    assert size(LotConfig(mode="fixed", fixed_lot=0.5))[0] == 0.5
    assert size(LotConfig(mode="multiplier", multiplier=1000, max_lot=3))[0] == 3
    assert size(LotConfig(mode="multiplier", multiplier=1000), slave_spec=spec(vmax=50))[0] == 50
    assert size(LotConfig(mode="multiplier", multiplier=0.5, min_lot=0.2))[0] == 0.2


def test_risk_percent():
    # 1% of 100k = 1000 USD; SL 50 pips on EURUSD = 500 points * $1/point/lot = $500 per lot -> 2 lots
    cfg = LotConfig(mode="risk_percent", risk_percent=1)
    assert size(cfg, entry_price=1.10000, sl_price=1.09500)[0] == 2.0
    assert size(cfg)[0] is None  # no SL -> skip
    assert size(LotConfig(mode="risk_percent", risk_no_sl="multiplier", multiplier=3))[0] == 0.3


def test_contract_size_normalisation():
    master = spec("XAUUSD", contract=100)
    slave = spec("GOLD", contract=10)
    assert size(LotConfig(multiplier=1), master_spec=master, slave_spec=slave)[0] == 1.0
    assert size(LotConfig(multiplier=1, normalize_contract_size=False), master_spec=master,
                slave_spec=slave)[0] == 0.1


def test_symbol_mapper():
    m = SymbolMapper(SymbolConfig(suffix="m", map={"XAUUSD": "GOLD"}), master_suffix=".r")
    assert m.to_slave("EURUSD.r") == "EURUSDm"
    assert m.to_slave("XAUUSD.r") == "GOLD"
    assert m.to_slave("XAUUSD") == "GOLD"
    assert m.fx_symbol("GBPUSD") == "GBPUSDm"
    f = SymbolMapper(SymbolConfig(include=["EURUSD", "GBPUSD"], exclude=["GBPUSD"]), master_suffix=".r")
    assert f.allowed("EURUSD.r") and not f.allowed("GBPUSD.r") and not f.allowed("USDJPY.r")


def test_conversion_rate():
    quotes = {"GBPUSD": Tick(1.27, 1.27), "USDJPY": Tick(150.0, 150.0)}
    q = quotes.get
    assert conversion_rate("USD", "USD", q) == 1.0
    assert conversion_rate("GBP", "USD", q) == pytest.approx(1.27)
    assert conversion_rate("USD", "GBP", q) == pytest.approx(1 / 1.27)
    assert conversion_rate("GBP", "JPY", q) == pytest.approx(190.5)
    assert conversion_rate("EUR", "USD", q) is None
    assert conversion_rate("EUR", "USD", q, overrides={"EURUSD": 1.1}) == 1.1


BASE = {
    "master": {"terminal_path": "C:/MT5/M/terminal64.exe"},
    "slaves": [{"name": "a", "terminal_path": "C:/MT5/A/terminal64.exe", "lots": {"mode": "multiplier", "multiplier": 10}}],
}


def test_config_parses_and_expands_env(monkeypatch):
    monkeypatch.setenv("PW", "secret")
    raw = {**BASE, "master": {**BASE["master"], "login": "123", "password": "${PW}", "server": "S"}}
    cfg = parse_config(raw)
    assert cfg.master.password == "secret" and cfg.master.login == 123
    assert cfg.slaves[0].lots.multiplier == 10


@pytest.mark.parametrize("mutate, msg", [
    (lambda r: r["slaves"][0].update(lotz={}), "unknown setting"),
    (lambda r: r["slaves"][0]["lots"].update(mode="bogus"), "lots.mode"),
    (lambda r: r["slaves"][0].update(terminal_path="C:/MT5/M/terminal64.exe"), "same terminal_path"),
    (lambda r: r["master"].update(password="${NOPE_NOT_SET}"), "NOPE_NOT_SET"),
    (lambda r: r["slaves"].append(dict(r["slaves"][0], terminal_path="other")), "unique"),
])
def test_config_errors(mutate, msg):
    import copy
    raw = copy.deepcopy(BASE)
    mutate(raw)
    os.environ.pop("NOPE_NOT_SET", None)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_example_config_is_valid(monkeypatch):
    from pathlib import Path
    from trade_copier.config import load_config
    for v in ("MT5_MASTER_PASSWORD", "MT5_SLAVE1_PASSWORD", "MT5_SLAVE2_PASSWORD"):
        monkeypatch.setenv(v, "x")
    cfg = load_config(Path(__file__).parent.parent / "config.example.yaml")
    assert [s.name for s in cfg.enabled_slaves] == ["big-account"]
