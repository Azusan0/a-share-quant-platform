#!/usr/bin/env python3
"""A股集合竞价阶段分析；缺失未匹配委托量时显式降级。"""
from __future__ import annotations

from datetime import time as _time
from typing import Any

from market_calendar import parse_shanghai


def _sample_time(value: Any):
    parsed = parse_shanghai(value)
    return parsed.time() if parsed else None


def analyze_auction(samples: list[dict[str, Any]], previous_close: float, sector_breadth_pct: float | None = None) -> dict[str, Any]:
    rows = sorted((row for row in samples if row.get("price")), key=lambda row: str(row.get("captured_at")))
    if not rows or previous_close <= 0:
        return {"signal": "unknown", "confidence": 0, "data_quality": "missing", "reasons": ["竞价快照缺失"]}
    first, last = rows[0], rows[-1]
    gap = (float(last["price"]) / previous_close - 1) * 100
    drift = (float(last["price"]) / float(first["price"]) - 1) * 100
    has_irrevocable = any((t := _sample_time(row.get("captured_at"))) and _time(9, 20) <= t <= _time(9, 25) for row in rows)
    reasons = [f"竞价相对昨收{gap:.2f}%", f"竞价阶段价格变化{drift:.2f}%"]
    score = gap * 12 + drift * 10
    if sector_breadth_pct is not None:
        score += (float(sector_breadth_pct) - 50) * .3
        reasons.append(f"板块上涨宽度{sector_breadth_pct:.1f}%")
    signal = "bullish" if score >= 20 else "bearish" if score <= -20 else "neutral"
    quality = "complete" if len(rows) >= 3 and has_irrevocable else "partial"
    confidence = min(85, 35 + len(rows) * 8 + (15 if has_irrevocable else 0))
    missing = [] if has_irrevocable else ["09:20后不可撤单阶段快照"]
    missing.append("未匹配委托量")
    return {"signal": signal, "score": round(score, 2), "confidence": confidence,
            "gap_pct": round(gap, 2), "price_drift_pct": round(drift, 2), "data_quality": quality,
            "reasons": reasons, "missing_data": missing, "samples": len(rows)}
