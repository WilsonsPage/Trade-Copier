"""Currency conversion using the slave terminal's own quotes."""

from __future__ import annotations

from typing import Callable, Optional

from .models import Tick


def conversion_rate(
    from_ccy: str,
    to_ccy: str,
    quote: Callable[[str], Optional[Tick]],
    symbol_name: Callable[[str], str] = lambda pair: pair,
    overrides: Optional[dict] = None,
) -> Optional[float]:
    """How many ``to_ccy`` one unit of ``from_ccy`` is worth.

    Tries, in order: a manual override, the direct pair, the inverse pair,
    and finally a cross via USD. Returns None when no rate can be found.
    """
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    overrides = {k.upper(): float(v) for k, v in (overrides or {}).items()}

    def direct(a: str, b: str) -> Optional[float]:
        if a == b:
            return 1.0
        if a + b in overrides:
            return overrides[a + b]
        if b + a in overrides and overrides[b + a] > 0:
            return 1.0 / overrides[b + a]
        tick = quote(symbol_name(a + b))
        if tick and tick.bid > 0 and tick.ask > 0:
            return (tick.bid + tick.ask) / 2
        tick = quote(symbol_name(b + a))
        if tick and tick.bid > 0 and tick.ask > 0:
            return 2 / (tick.bid + tick.ask)
        return None

    rate = direct(from_ccy, to_ccy)
    if rate is not None:
        return rate
    if "USD" not in (from_ccy, to_ccy):
        a = direct(from_ccy, "USD")
        b = direct("USD", to_ccy)
        if a is not None and b is not None:
            return a * b
    return None
