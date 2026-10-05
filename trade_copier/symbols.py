"""Translating master symbol names into the slave broker's names."""

from __future__ import annotations

from .config import SymbolConfig


class SymbolMapper:
    """Maps a master symbol (e.g. ``EURUSD.r``) to the slave's (e.g. ``EURUSDm``).

    Order of precedence:
      1. an explicit ``map`` entry for the raw master symbol;
      2. an explicit ``map`` entry for the base symbol (master prefix/suffix stripped);
      3. slave prefix + base symbol + slave suffix.
    """

    def __init__(self, cfg: SymbolConfig, master_prefix: str = "", master_suffix: str = ""):
        self.cfg = cfg
        self.master_prefix = master_prefix or ""
        self.master_suffix = master_suffix or ""
        self._map = {k.upper(): v for k, v in cfg.map.items()}
        self._include = {s.upper() for s in cfg.include}
        self._exclude = {s.upper() for s in cfg.exclude}

    def base(self, master_symbol: str) -> str:
        s = master_symbol
        if self.master_prefix and s.startswith(self.master_prefix):
            s = s[len(self.master_prefix):]
        if self.master_suffix and s.endswith(self.master_suffix):
            s = s[: -len(self.master_suffix)]
        return s

    def to_slave(self, master_symbol: str) -> str:
        if master_symbol.upper() in self._map:
            return self._map[master_symbol.upper()]
        base = self.base(master_symbol)
        if base.upper() in self._map:
            return self._map[base.upper()]
        return f"{self.cfg.prefix}{base}{self.cfg.suffix}"

    def allowed(self, master_symbol: str) -> bool:
        names = {master_symbol.upper(), self.base(master_symbol).upper()}
        if self._include and not (names & self._include):
            return False
        return not (names & self._exclude)

    def fx_symbol(self, pair: str) -> str:
        """Slave-side name of a currency pair such as ``GBPUSD``."""
        if pair.upper() in self._map:
            return self._map[pair.upper()]
        return f"{self.cfg.prefix}{pair}{self.cfg.suffix}"
