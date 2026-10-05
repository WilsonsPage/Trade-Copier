"""Persistent per-slave state: which master trade maps to which slave trade."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

COMMENT_TAG = "TC|"


def make_tag(master_id: int) -> str:
    return f"{COMMENT_TAG}{master_id}"


def parse_tag(comment: str) -> Optional[int]:
    if comment and comment.startswith(COMMENT_TAG):
        try:
            return int(comment[len(COMMENT_TAG):].split()[0])
        except (ValueError, IndexError):
            return None
    return None


@dataclass
class Link:
    master_id: int
    slave_id: int
    kind: str  # "position" or "order"
    symbol: str
    master_volume: float
    slave_volume: float
    status: str = "open"  # "open" or "slave_closed" (closed on the slave by hand / SL)
    created: float = field(default_factory=time.time)


class SlaveState:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.links: dict = {}  # master_id -> Link
        self.known: set = set()  # master ids we've decided not to copy (pre-existing)
        self.skipped: set = set()  # master ids we tried and gave up on / filtered out
        self.seeded = False
        self.equity_peak = 0.0
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> "SlaveState":
        state = cls(path)
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            state.links = {int(k): Link(**v) for k, v in data.get("links", {}).items()}
            state.known = set(data.get("known", []))
            state.skipped = set(data.get("skipped", []))
            state.seeded = bool(data.get("seeded", False))
            state.equity_peak = float(data.get("equity_peak", 0.0))
        return state

    def save(self, force: bool = False) -> None:
        if not self.path or not (self.dirty or force):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "links": {str(k): asdict(v) for k, v in self.links.items()},
            "known": sorted(self.known),
            "skipped": sorted(self.skipped),
            "seeded": self.seeded,
            "equity_peak": self.equity_peak,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False
