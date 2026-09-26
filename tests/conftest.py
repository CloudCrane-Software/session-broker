# coding: utf-8
"""pytest 共享夹具：src 路径 + 冻结时钟."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FROZEN_NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """可手动推进的时钟（默认冻结在 FROZEN_NOW）。"""

    def __init__(self, start=None):
        self.now = start or FROZEN_NOW

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)
        return self.now
