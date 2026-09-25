#!/usr/bin/env python3
"""A股交易时段与快照新鲜度工具。"""
from __future__ import annotations

from datetime import datetime

from market_calendar import is_fresh as _is_fresh
from market_calendar import is_trading_session as _is_trading_session
from market_calendar import parse_shanghai


def is_trading_session(now: datetime | None = None) -> bool:
    return _is_trading_session(now)


def parse_time(value: object) -> datetime | None:
    return parse_shanghai(value)


def is_fresh(value: object, now: datetime | None = None, max_age_minutes: int = 12) -> bool:
    return _is_fresh(value, now, max_age_minutes)
