"""Loading and validating the YAML configuration file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(ValueError):
    pass


LOT_MODES = ("multiplier", "balance_ratio", "equity_ratio", "fixed", "risk_percent")
DIRECTIONS = ("both", "buy_only", "sell_only")


@dataclass
class TerminalConfig:
    """How to reach one MetaTrader 5 terminal (one terminal per account)."""

    terminal_path: str = ""
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    portable: bool = False


@dataclass
class Settings:
    poll_interval_ms: int = 250
    heartbeat_seconds: float = 1.0
    max_snapshot_age_seconds: float = 5.0
    state_dir: str = "state"
    log_dir: str = "logs"
    log_level: str = "INFO"


@dataclass
class MasterConfig(TerminalConfig):
    name: str = "master"
    symbol_prefix: str = ""
    symbol_suffix: str = ""
    only_manual_trades: bool = False


@dataclass
class LotConfig:
    mode: str = "multiplier"
    multiplier: float = 1.0
    fixed_lot: float = 0.01
    risk_percent: float = 1.0
    risk_no_sl: str = "skip"  # "skip" or "multiplier"
    min_lot: Optional[float] = None
    max_lot: Optional[float] = None
    round_up_to_min_lot: bool = False
    normalize_contract_size: bool = True


@dataclass
class RiskConfig:
    copy_sl: bool = True
    copy_tp: bool = True
    slippage_points: int = 20
    max_price_deviation_points: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_drawdown_percent: Optional[float] = None
    max_retries: int = 5


@dataclass
class SymbolConfig:
    prefix: str = ""
    suffix: str = ""
    map: dict = field(default_factory=dict)
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)


@dataclass
class SlaveConfig(TerminalConfig):
    name: str = ""
    enabled: bool = True
    magic: int = 770077
    dry_run: bool = False
    copy_existing_on_start: bool = False
    copy_pending_orders: bool = True
    pending_fill_to_market: bool = True
    reverse: bool = False
    direction: str = "both"
    lots: LotConfig = field(default_factory=LotConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    symbols: SymbolConfig = field(default_factory=SymbolConfig)
    fx_rates: dict = field(default_factory=dict)


@dataclass
class Config:
    master: MasterConfig
    slaves: list
    settings: Settings = field(default_factory=Settings)

    @property
    def enabled_slaves(self) -> list:
        return [s for s in self.slaves if s.enabled]


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, where: str) -> Any:
    """Replace ${VAR} references with environment variables (for passwords)."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"{where}: environment variable {name} is not set")
            return os.environ[name]

        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


_NESTED = {"lots": LotConfig, "risk": RiskConfig, "symbols": SymbolConfig}


def _build(cls, data: Any, where: str):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(unknown)}")
    kwargs = {}
    for key, value in data.items():
        if key in _NESTED and cls is SlaveConfig:
            value = _build(_NESTED[key], value, f"{where}.{key}")
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{where}: {exc}") from exc


def _validate_terminal(t: TerminalConfig, where: str) -> None:
    if not t.terminal_path:
        raise ConfigError(f"{where}: terminal_path is required (path to terminal64.exe)")
    if t.login is not None:
        try:
            t.login = int(t.login)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: login must be a number") from None
        if not t.password or not t.server:
            raise ConfigError(f"{where}: password and server are required when login is set")


def _validate_slave(s: SlaveConfig, where: str) -> None:
    if not s.name:
        raise ConfigError(f"{where}: name is required")
    _validate_terminal(s, where)
    if s.direction not in DIRECTIONS:
        raise ConfigError(f"{where}.direction must be one of {', '.join(DIRECTIONS)}")
    lots = s.lots
    if lots.mode not in LOT_MODES:
        raise ConfigError(f"{where}.lots.mode must be one of {', '.join(LOT_MODES)}")
    if lots.multiplier <= 0:
        raise ConfigError(f"{where}.lots.multiplier must be greater than 0")
    if lots.mode == "fixed" and lots.fixed_lot <= 0:
        raise ConfigError(f"{where}.lots.fixed_lot must be greater than 0")
    if lots.mode == "risk_percent" and not (0 < lots.risk_percent <= 100):
        raise ConfigError(f"{where}.lots.risk_percent must be between 0 and 100")
    if lots.risk_no_sl not in ("skip", "multiplier"):
        raise ConfigError(f"{where}.lots.risk_no_sl must be 'skip' or 'multiplier'")
    if lots.min_lot is not None and lots.max_lot is not None and lots.min_lot > lots.max_lot:
        raise ConfigError(f"{where}.lots.min_lot is larger than max_lot")
    if s.risk.max_drawdown_percent is not None and not (0 < s.risk.max_drawdown_percent < 100):
        raise ConfigError(f"{where}.risk.max_drawdown_percent must be between 0 and 100")
    if not isinstance(s.symbols.map, dict):
        raise ConfigError(f"{where}.symbols.map must be a mapping like {{XAUUSD: GOLD}}")
    s.symbols.map = {str(k): str(v) for k, v in s.symbols.map.items()}
    s.fx_rates = {str(k).upper(): float(v) for k, v in (s.fx_rates or {}).items()}


def parse_config(raw: dict, expand_env: bool = True) -> Config:
    """Build a Config from the parsed YAML.

    ``expand_env=False`` leaves ``${VAR}`` references as they are, which the
    control panel uses to validate a config without needing the passwords.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a mapping at the top level")
    unknown = sorted(set(raw) - {"master", "slaves", "settings"})
    if unknown:
        raise ConfigError(f"unknown top-level section(s) {', '.join(unknown)}")
    if expand_env:
        raw = _expand_env(raw, "config")

    settings = _build(Settings, raw.get("settings"), "settings")
    if "master" not in raw:
        raise ConfigError("a 'master' section is required")
    master = _build(MasterConfig, raw["master"], "master")
    _validate_terminal(master, "master")

    slaves_raw = raw.get("slaves") or []
    if not isinstance(slaves_raw, list) or not slaves_raw:
        raise ConfigError("'slaves' must be a list with at least one slave account")
    slaves = []
    for i, item in enumerate(slaves_raw):
        where = f"slaves[{i}]"
        slave = _build(SlaveConfig, item, where)
        _validate_slave(slave, where)
        slaves.append(slave)

    names = [s.name for s in slaves]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"slave names must be unique: {', '.join(sorted(dupes))}")

    terminals = [master, *[s for s in slaves if s.enabled]]
    seen: dict = {}
    for t in terminals:
        key = os.path.normcase(os.path.normpath(t.terminal_path))
        if key in seen:
            raise ConfigError(
                f"'{getattr(t, 'name', '?')}' and '{seen[key]}' use the same terminal_path; "
                "each account needs its own MetaTrader 5 installation"
            )
        seen[key] = getattr(t, "name", "?")

    return Config(master=master, slaves=slaves, settings=settings)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"could not parse {path}: {exc}") from exc
    return parse_config(raw)
