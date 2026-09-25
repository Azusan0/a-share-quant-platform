#!/usr/bin/env python3
"""统一 A 股交易时钟、交易日历和上海时间解析。

历史 JSON/SQLite 中已经保存了大量 naive ISO 时间；本模块统一把这些
naive 时间解释为 Asia/Shanghai，避免迁移到 UTC 服务器后发生时段错判。
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_TRADE_DATES_CACHE: set[str] | None = None


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def as_shanghai(value: datetime | None = None) -> datetime:
    value = value or now_shanghai()
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def parse_shanghai(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_shanghai(value)
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return as_shanghai(parsed)


def _load_trade_dates_from_file(path: Path) -> set[str] | None:
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return None


def load_trade_dates() -> set[str] | None:
    global _TRADE_DATES_CACHE
    if _TRADE_DATES_CACHE is not None:
        return _TRADE_DATES_CACHE
    cache_file = os.environ.get("A_SHARE_TRADE_DATES_FILE")
    if cache_file:
        cached = _load_trade_dates_from_file(Path(cache_file))
        if cached:
            _TRADE_DATES_CACHE = cached
            return cached
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        _TRADE_DATES_CACHE = {str(day) for day in frame["trade_date"].astype(str)}
        return _TRADE_DATES_CACHE
    except Exception:
        return None


def is_trade_date(day: date | datetime | None = None) -> bool:
    current = as_shanghai(day if isinstance(day, datetime) else None).date() if day is None or isinstance(day, datetime) else day
    trade_dates = load_trade_dates()
    if trade_dates is None:
        return current.weekday() < 5
    return current.isoformat() in trade_dates


def market_phase(value: datetime | None = None) -> str:
    now = as_shanghai(value)
    if not is_trade_date(now.date()):
        return "closed"
    current = now.time()
    if current < time(9, 15):
        return "premarket"
    if time(9, 15) <= current < time(9, 20):
        return "auction_cancelable"
    if time(9, 20) <= current < time(9, 25):
        return "auction_locked"
    if time(9, 25) <= current < time(9, 30):
        return "opening"
    if time(9, 30) <= current <= time(11, 30):
        return "intraday"
    if time(11, 30) < current < time(13, 0):
        return "intraday_break"
    if time(13, 0) <= current <= time(15, 0):
        return "intraday"
    if time(15, 0) < current < time(15, 15):
        return "after_close"
    return "overnight"


def is_trading_session(value: datetime | None = None) -> bool:
    return market_phase(value) == "intraday"


def is_fresh(value: Any, now: datetime | None = None, max_age_minutes: int = 12) -> bool:
    current = as_shanghai(now)
    timestamp = parse_shanghai(value)
    if timestamp is None or timestamp.date() != current.date():
        return False
    age = current - timestamp
    return timedelta(0) <= age <= timedelta(minutes=max_age_minutes)
