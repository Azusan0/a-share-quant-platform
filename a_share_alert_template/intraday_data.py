#!/usr/bin/env python3
"""5分钟K线多源适配器。

腾讯为主源，新浪为备用源；两者统一输出成交量（股）和成交额。备用源仅在
主源失败或主源行情过期时启用，调用方可通过 ``attempts`` 落库健康记录。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TENCENT_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
REFERER = "https://gu.qq.com/"
SINA_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketData.getKLineData"
SINA_REFERER = "https://finance.sina.com.cn/"


class IntradayDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchResult:
    symbol: str
    provider: str
    bars: list[dict[str, Any]]
    fetched_at: str
    latency_ms: int
    stale: bool
    fallback_level: int = 0
    attempts: tuple[dict[str, Any], ...] = ()


def market_code(symbol: str) -> str:
    """把六位证券代码路由到腾讯/新浪市场前缀，并拒绝不明确的代码。"""
    raw = str(symbol).strip().lower()
    supplied = ""
    if raw.startswith(("sh", "sz", "bj")):
        supplied, raw = raw[:2], raw[2:]
    if len(raw) != 6 or not raw.isdigit():
        raise ValueError(f"无效证券代码: {symbol}")
    first = raw[0]
    if first in {"5", "6", "9"}:
        inferred = "sh"
    elif first in {"0", "1", "2", "3"}:
        inferred = "sz"
    elif first in {"4", "8"}:
        inferred = "bj"
    else:
        raise ValueError(f"无法判断证券市场: {symbol}")
    if supplied and supplied != inferred:
        raise ValueError(f"证券代码与市场前缀矛盾: {symbol}")
    return inferred + raw


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise IntradayDataError(f"分钟K字段 {field} 非数字") from exc
    if not math.isfinite(number):
        raise IntradayDataError(f"分钟K字段 {field} 非有限值")
    return number


def _valid_bar_time(bar_time: datetime, now: datetime) -> bool:
    minute = bar_time.hour * 60 + bar_time.minute
    return bar_time <= now + timedelta(minutes=2) and (570 < minute <= 690 or 780 <= minute <= 900)


def _valid_price(open_price: float, close: float, high: float, low: float) -> bool:
    return min(open_price, close, high, low) > 0 and high >= max(open_price, close, low) and low <= min(open_price, close, high)


def parse_tencent_payload(payload: dict[str, Any], symbol: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    routed = market_code(symbol)
    root = payload.get("data") if isinstance(payload, dict) else None
    node = root.get(routed) if isinstance(root, dict) else None
    rows = node.get("m5") if isinstance(node, dict) else None
    if not isinstance(rows, list) or not rows:
        raise IntradayDataError(f"腾讯未返回 {symbol} 的5分钟K线")
    parsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            bar_time = datetime.strptime(str(row[0]), "%Y%m%d%H%M")
            open_price, close, high, low = (_number(row[index], field) for index, field in ((1, "open"), (2, "close"), (3, "high"), (4, "low")))
            volume_lots = _number(row[5], "volume")
        except (ValueError, IntradayDataError):
            continue
        if not _valid_bar_time(bar_time, now) or not _valid_price(open_price, close, high, low) or volume_lots < 0:
            continue
        volume_shares = int(round(volume_lots * 100))
        parsed[bar_time.isoformat(timespec="minutes")] = {
            "symbol": routed[2:], "time": bar_time.isoformat(timespec="minutes"),
            "open": open_price, "close": close, "high": high, "low": low,
            "volume_shares": volume_shares,
            "amount_estimated": round(volume_shares * (open_price + close + high + low) / 4, 2),
            "provider": "tencent_m5",
        }
    bars = sorted(parsed.values(), key=lambda item: item["time"])
    if not bars:
        raise IntradayDataError(f"腾讯 {symbol} 的5分钟K线全部未通过校验")
    return bars


def parse_sina_payload(payload: str, symbol: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """解析新浪JSONP分钟K线，使用其原始成交量与成交额字段。"""
    now = now or datetime.now()
    routed = market_code(symbol)
    start, end = payload.find("["), payload.rfind("]")
    if start < 0 or end < start:
        raise IntradayDataError(f"新浪未返回 {symbol} 的5分钟K线")
    try:
        rows = json.loads(payload[start:end + 1])
    except json.JSONDecodeError as exc:
        raise IntradayDataError(f"新浪5分钟K线解析失败 {symbol}") from exc
    if not isinstance(rows, list) or not rows:
        raise IntradayDataError(f"新浪未返回 {symbol} 的5分钟K线")
    parsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            bar_time = datetime.strptime(str(row.get("day") or ""), "%Y-%m-%d %H:%M:%S")
            open_price, close, high, low = (_number(row.get(field), field) for field in ("open", "close", "high", "low"))
            volume_shares = int(round(_number(row.get("volume"), "volume")))
            amount = _number(row.get("amount"), "amount")
        except (ValueError, IntradayDataError):
            continue
        if not _valid_bar_time(bar_time, now) or not _valid_price(open_price, close, high, low) or volume_shares < 0 or amount < 0:
            continue
        parsed[bar_time.isoformat(timespec="minutes")] = {
            "symbol": routed[2:], "time": bar_time.isoformat(timespec="minutes"),
            "open": open_price, "close": close, "high": high, "low": low,
            "volume_shares": volume_shares, "amount_estimated": round(amount, 2), "provider": "sina_m5",
        }
    bars = sorted(parsed.values(), key=lambda item: item["time"])
    if not bars:
        raise IntradayDataError(f"新浪 {symbol} 的5分钟K线全部未通过校验")
    return bars


def _is_stale(latest: datetime, now: datetime) -> bool:
    """只在当日交易时段判断延迟，盘后保留最后一根15:00 K线不算异常。"""
    if latest.date() != now.date():
        return now.weekday() < 5
    minute = now.hour * 60 + now.minute
    if 570 <= minute <= 690 or 780 <= minute <= 900:
        return now - latest > timedelta(minutes=12)
    return False


def fetch_m5(symbol: str, limit: int = 80, timeout: float = 6.0, retries: int = 2, now: datetime | None = None) -> FetchResult:
    now = now or datetime.now()
    routed = market_code(symbol)
    limit = max(10, min(int(limit), 320))
    request = Request(f"{TENCENT_URL}?{urlencode({'param': f'{routed},m5,,{limit}'})}", headers={"Referer": REFERER, "User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            bars = parse_tencent_payload(payload, routed, now=now)
            latency = int((time.perf_counter() - started) * 1000)
            return FetchResult(routed[2:], "tencent_m5", bars, now.isoformat(timespec="seconds"), latency, _is_stale(datetime.fromisoformat(bars[-1]["time"]), now))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, IntradayDataError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.35 * (attempt + 1))
    raise IntradayDataError(f"腾讯5分钟K获取失败 {symbol}: {last_error}")


def fetch_sina_m5(symbol: str, limit: int = 80, timeout: float = 6.0, retries: int = 1, now: datetime | None = None) -> FetchResult:
    now = now or datetime.now()
    routed = market_code(symbol)
    limit = max(10, min(int(limit), 320))
    query = urlencode({"symbol": routed, "scale": 5, "ma": "no", "datalen": limit})
    request = Request(f"{SINA_URL}?{query}", headers={"Referer": SINA_REFERER, "User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("gb18030", errors="replace")
            bars = parse_sina_payload(payload, routed, now=now)
            latency = int((time.perf_counter() - started) * 1000)
            return FetchResult(routed[2:], "sina_m5", bars, now.isoformat(timespec="seconds"), latency, _is_stale(datetime.fromisoformat(bars[-1]["time"]), now), 1)
        except (HTTPError, URLError, TimeoutError, IntradayDataError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.35 * (attempt + 1))
    raise IntradayDataError(f"新浪5分钟K获取失败 {symbol}: {last_error}")


def _attempt(result: FetchResult | None, provider: str, error: Exception | None = None) -> dict[str, Any]:
    return {"provider": provider, "ok": result is not None, "latency_ms": result.latency_ms if result else None,
            "stale": result.stale if result else False, "fallback_level": result.fallback_level if result else 0,
            "error": str(error)[:500] if error else None}


def fetch_m5_with_fallback(symbol: str, limit: int = 80, timeout: float = 6.0, retries: int = 2, now: datetime | None = None) -> FetchResult:
    """优先腾讯；腾讯失败或过期时用新浪补救，并保留双源健康记录。"""
    now = now or datetime.now()
    primary: FetchResult | None = None
    primary_error: Exception | None = None
    try:
        primary = fetch_m5(symbol, limit, timeout, retries, now)
        if not primary.stale:
            return FetchResult(**{**primary.__dict__, "attempts": (_attempt(primary, primary.provider),)})
    except IntradayDataError as exc:
        primary_error = exc
    try:
        fallback = fetch_sina_m5(symbol, limit, timeout, max(0, retries - 1), now)
        attempts = (_attempt(primary, "tencent_m5", primary_error), _attempt(fallback, fallback.provider))
        return FetchResult(**{**(fallback if not fallback.stale or primary is None else primary).__dict__, "attempts": attempts})
    except IntradayDataError as fallback_error:
        if primary is not None:
            return FetchResult(**{**primary.__dict__, "attempts": (_attempt(primary, primary.provider), _attempt(None, "sina_m5", fallback_error))})
        raise IntradayDataError(f"分钟K双源均失败 {symbol}: 腾讯={primary_error}; 新浪={fallback_error}") from fallback_error
