#!/usr/bin/env python3
"""基于已收盘日线与盘中快照生成可解释的多周期技术诊断。"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _latest_unfilled_gaps(frame: pd.DataFrame, price: float) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for index in range(1, len(frame)):
        previous = frame.iloc[index - 1]
        current = frame.iloc[index]
        later = frame.iloc[index + 1 :]
        previous_high = _number(previous.get("high"))
        previous_low = _number(previous.get("low"))
        current_high = _number(current.get("high"))
        current_low = _number(current.get("low"))
        if None in (previous_high, previous_low, current_high, current_low):
            continue
        date = str(current.get("date"))[:10]
        if current_low > previous_high * 1.003:
            later_low = _number(pd.to_numeric(later.get("low"), errors="coerce").min()) if not later.empty else price
            if min(later_low if later_low is not None else price, price) > previous_high:
                gaps.append({"date": date, "type": "up", "low": previous_high, "high": current_low})
        elif current_high < previous_low * 0.997:
            later_high = _number(pd.to_numeric(later.get("high"), errors="coerce").max()) if not later.empty else price
            if max(later_high if later_high is not None else price, price) < previous_low:
                gaps.append({"date": date, "type": "down", "low": current_high, "high": previous_low})
    return [
        {**gap, "low": _round(gap["low"]), "high": _round(gap["high"])}
        for gap in gaps[-3:]
    ]


def _overhead_supply_zone(frame: pd.DataFrame, price: float) -> dict[str, float] | None:
    highs = pd.to_numeric(frame.get("high"), errors="coerce")
    lows = pd.to_numeric(frame.get("low"), errors="coerce")
    closes = pd.to_numeric(frame.get("close"), errors="coerce")
    volumes = pd.to_numeric(frame.get("volume"), errors="coerce")
    typical = (highs + lows + closes) / 3
    valid = typical.notna() & volumes.notna() & (volumes > 0) & (typical > price)
    if valid.sum() < 3:
        return None
    upper = float(typical[valid].max())
    if upper <= price * 1.003:
        return None
    edges = np.linspace(price, upper, 13)
    buckets = pd.cut(typical[valid], bins=edges, include_lowest=True, duplicates="drop")
    grouped = volumes[valid].groupby(buckets, observed=True).sum()
    if grouped.empty:
        return None
    interval = grouped.idxmax()
    return {
        "low": round(float(interval.left), 3),
        "high": round(float(interval.right), 3),
        "volume_share_pct": round(float(grouped.max() / grouped.sum() * 100), 1),
    }


def diagnose_technical(history: pd.DataFrame, snapshot: dict[str, Any]) -> dict[str, Any]:
    """返回趋势、波动、支撑压力以及结构化正反证据。"""
    price = _number(snapshot.get("price"))
    if history is None or history.empty or price is None or price <= 0:
        return {
            "technical_score": None,
            "trend": "unknown",
            "support_evidence": [],
            "opposing_evidence": [],
            "missing_data": ["有效日线或最新价"],
            "data_quality": "missing",
        }

    frame = history.copy().tail(65).reset_index(drop=True)
    for column in ("open", "close", "high", "low", "volume"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    closes = frame["close"].dropna()
    highs = frame["high"].dropna()
    lows = frame["low"].dropna()
    missing: list[str] = []
    if len(closes) < 20 or len(highs) < 20 or len(lows) < 20:
        return {
            "technical_score": None,
            "trend": "unknown",
            "support_evidence": [],
            "opposing_evidence": [],
            "missing_data": ["至少20根完整日线"],
            "data_quality": "missing",
        }

    moving_averages: dict[str, float | None] = {}
    for period in (5, 10, 20, 60):
        moving_averages[f"ma{period}"] = float(closes.tail(period).mean()) if len(closes) >= period else None
        if len(closes) < period:
            missing.append(f"MA{period}")

    previous_close = closes.shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = _number(true_range.tail(14).mean()) if true_range.notna().sum() >= 14 else None
    if atr14 is None:
        missing.append("ATR14")
    atr_pct = atr14 / price * 100 if atr14 is not None else None

    daily_returns = closes.pct_change().dropna()
    volatility20 = float(daily_returns.tail(20).std(ddof=0) * math.sqrt(252) * 100) if len(daily_returns) >= 20 else None
    if volatility20 is None:
        missing.append("20日年化波动率")
    return5 = (price / float(closes.iloc[-5]) - 1) * 100 if len(closes) >= 5 else None

    ma5 = moving_averages["ma5"]
    ma10 = moving_averages["ma10"]
    ma20 = moving_averages["ma20"]
    ma60 = moving_averages["ma60"]
    ma20_previous = float(closes.iloc[-25:-5].mean()) if len(closes) >= 25 else None
    ma60_previous = float(closes.iloc[-65:-5].mean()) if len(closes) >= 65 else None
    ma20_slope_pct = (ma20 / ma20_previous - 1) * 100 if ma20 and ma20_previous else None
    ma60_slope_pct = (ma60 / ma60_previous - 1) * 100 if ma60 and ma60_previous else None

    short_bullish = bool(ma5 and ma10 and price > ma5 > ma10)
    medium_bullish = bool(ma20 and price > ma20 and (ma20_slope_pct or 0) > 0)
    long_bullish = bool(ma60 and price > ma60 and (ma60_slope_pct or 0) > 0)
    bearish_count = sum([
        bool(ma5 and ma10 and price < ma5 < ma10),
        bool(ma20 and price < ma20 and (ma20_slope_pct or 0) < 0),
        bool(ma60 and price < ma60 and (ma60_slope_pct or 0) < 0),
    ])
    bullish_count = sum((short_bullish, medium_bullish, long_bullish))
    trend = "bullish" if bullish_count >= 2 and bearish_count == 0 else "bearish" if bearish_count >= 2 else "mixed"

    high20 = float(highs.tail(20).max())
    low20 = float(lows.tail(20).min())
    high60 = float(highs.tail(60).max()) if len(highs) >= 60 else None
    low60 = float(lows.tail(60).min()) if len(lows) >= 60 else None
    supports = [(name, value) for name, value in (
        ("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60),
        ("20日低点", low20), ("60日低点", low60),
    ) if value is not None and value < price]
    resistances = [(name, value) for name, value in (
        ("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60),
        ("20日高点", high20), ("60日高点", high60),
    ) if value is not None and value > price]
    support_name, support_price = max(supports, key=lambda item: item[1]) if supports else (None, None)
    resistance_name, resistance_price = min(resistances, key=lambda item: item[1]) if resistances else (None, None)
    support_distance = (price / support_price - 1) * 100 if support_price else None
    resistance_distance = (resistance_price / price - 1) * 100 if resistance_price else None

    gaps = _latest_unfilled_gaps(frame.tail(60), price)
    supply_zone = _overhead_supply_zone(frame.tail(60), price)
    if supply_zone is None and pd.to_numeric(frame["volume"], errors="coerce").notna().sum() < 20:
        missing.append("成交量分布/套牢区")

    support_evidence: list[str] = []
    opposing_evidence: list[str] = []
    score = 50.0
    if short_bullish:
        support_evidence.append("短期均线多头排列")
        score += 8
    if medium_bullish:
        support_evidence.append("价格站上上行MA20")
        score += 12
    if long_bullish:
        support_evidence.append("价格站上上行MA60")
        score += 10
    if support_distance is not None and support_distance <= 3:
        support_evidence.append(f"距{support_name}支撑{support_distance:.1f}%")
        score += 8
    if return5 is not None and 0 <= return5 <= 8:
        support_evidence.append(f"5日涨幅{return5:.1f}%仍属温和")
        score += 5
    if bearish_count >= 2:
        opposing_evidence.append("两个以上周期处于空头结构")
        score -= 20
    elif ma20 and price < ma20:
        opposing_evidence.append("价格仍在MA20下方")
        score -= 10
    if atr_pct is not None and atr_pct >= 5:
        opposing_evidence.append(f"ATR14达{atr_pct:.1f}%，波动偏高")
        score -= 8
    if return5 is not None and return5 >= 15:
        opposing_evidence.append(f"5日累计上涨{return5:.1f}%，追涨风险高")
        score -= 12
    if resistance_distance is not None and resistance_distance <= 2:
        opposing_evidence.append(f"距{resistance_name}压力仅{resistance_distance:.1f}%")
        score -= 8
    if supply_zone and supply_zone["low"] <= price * 1.03:
        opposing_evidence.append("上方3%内存在高成交量套牢区")
        score -= 6
    for gap in gaps:
        if gap["type"] == "down" and gap["low"] > price:
            opposing_evidence.append("上方存在未回补向下缺口")
            score -= 5
            break

    metrics = {
        **{key: _round(value) for key, value in moving_averages.items()},
        "ma20_slope_pct": _round(ma20_slope_pct, 2),
        "ma60_slope_pct": _round(ma60_slope_pct, 2),
        "atr14": _round(atr14),
        "atr14_pct": _round(atr_pct, 2),
        "volatility20_pct": _round(volatility20, 2),
        "return5_pct": _round(return5, 2),
        "high20": _round(high20), "low20": _round(low20),
        "high60": _round(high60), "low60": _round(low60),
    }
    return {
        "technical_score": round(max(0, min(100, score)), 1),
        "trend": trend,
        "trend_periods": {
            "short": "bullish" if short_bullish else "bearish" if price < (ma5 or price) else "mixed",
            "medium": "bullish" if medium_bullish else "bearish" if ma20 and price < ma20 else "mixed",
            "long": "bullish" if long_bullish else "bearish" if ma60 and price < ma60 else "mixed",
        },
        "support_name": support_name,
        "support_price": _round(support_price),
        "support_distance_pct": _round(support_distance, 2),
        "resistance_name": resistance_name,
        "resistance_price": _round(resistance_price),
        "resistance_distance_pct": _round(resistance_distance, 2),
        "unfilled_gaps": gaps,
        "overhead_supply_zone": supply_zone,
        "metrics": metrics,
        "support_evidence": support_evidence,
        "opposing_evidence": opposing_evidence,
        "missing_data": missing,
        "data_quality": "complete" if not missing else "partial",
    }
