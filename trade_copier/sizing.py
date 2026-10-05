"""Working out the slave's lot size for a copied trade."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import LotConfig
from .models import AccountSnap, SymbolSpec

_EPS = 1e-9


@dataclass
class SizingInput:
    master_volume: float
    master_account: AccountSnap
    slave_account: AccountSnap
    slave_spec: SymbolSpec
    master_spec: Optional[SymbolSpec] = None
    fx_master_to_slave: Optional[float] = None  # 1 master ccy = X slave ccy
    entry_price: float = 0.0
    sl_price: float = 0.0  # stop loss as it will be placed on the slave (0 = none)


def round_volume(volume: float, spec: SymbolSpec, mode: str = "down") -> float:
    """Snap a volume onto the symbol's volume_step grid."""
    step = spec.volume_step or 0.01
    steps = volume / step
    n = math.floor(steps + 1e-6) if mode == "down" else math.floor(steps + 0.5)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


def compute_volume(cfg: LotConfig, inp: SizingInput) -> tuple:
    """Return ``(volume, None)`` or ``(None, reason)`` when the trade should be skipped."""
    spec = inp.slave_spec
    mode = cfg.mode

    if mode == "risk_percent":
        if inp.sl_price <= 0 or inp.entry_price <= 0:
            if cfg.risk_no_sl == "skip":
                return None, "risk_percent sizing needs a stop loss and the master trade has none"
            mode = "multiplier"
        else:
            if spec.tick_size <= 0 or spec.tick_value <= 0:
                return None, f"{spec.name} reports no tick value, cannot size by risk"
            risk_money = inp.slave_account.equity * cfg.risk_percent / 100.0
            loss_per_lot = abs(inp.entry_price - inp.sl_price) / spec.tick_size * spec.tick_value
            if loss_per_lot <= 0:
                return None, "stop loss distance is zero"
            volume = risk_money / loss_per_lot
            return _finish(cfg, spec, volume)

    if mode == "fixed":
        return _finish(cfg, spec, cfg.fixed_lot)

    volume = inp.master_volume * cfg.multiplier
    if mode in ("balance_ratio", "equity_ratio"):
        if mode == "balance_ratio":
            master_size, slave_size = inp.master_account.balance, inp.slave_account.balance
        else:
            master_size, slave_size = inp.master_account.equity, inp.slave_account.equity
        if inp.fx_master_to_slave is None:
            return None, (
                f"no exchange rate from {inp.master_account.currency} to "
                f"{inp.slave_account.currency}; add it under fx_rates"
            )
        master_in_slave_ccy = master_size * inp.fx_master_to_slave
        if master_in_slave_ccy <= 0:
            return None, "master account size is zero"
        volume *= slave_size / master_in_slave_ccy

    if cfg.normalize_contract_size and inp.master_spec is not None:
        if inp.master_spec.contract_size > 0 and spec.contract_size > 0:
            volume *= inp.master_spec.contract_size / spec.contract_size

    return _finish(cfg, spec, volume)


def _finish(cfg: LotConfig, spec: SymbolSpec, volume: float) -> tuple:
    if cfg.max_lot is not None:
        volume = min(volume, cfg.max_lot)
    if cfg.min_lot is not None:
        volume = max(volume, cfg.min_lot)
    volume = round_volume(volume, spec, "down")
    if volume < spec.volume_min - _EPS:
        if cfg.round_up_to_min_lot:
            volume = spec.volume_min
        else:
            return None, (
                f"calculated lot is below the broker minimum of {spec.volume_min} "
                "(set lots.round_up_to_min_lot: true to trade the minimum instead)"
            )
    if spec.volume_max > 0 and volume > spec.volume_max:
        volume = round_volume(spec.volume_max, spec, "down")
    return volume, None
