#!/usr/bin/env python3
"""基于5分钟K线的确定性分时状态机。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


LABELS = {
    "dormant": "休眠",
    "setup": "蓄势",
    "ignition": "启动",
    "confirmation": "确认",
    "pullback": "回踩",
    "second_ignition": "二次启动",
    "overheated": "过热",
    "failed": "失效",
}


def _metrics(bars: list[dict[str, Any]]) -> dict[str, float]:
    closes = [float(row["close"]) for row in bars]
    highs = [float(row["high"]) for row in bars]
    volumes = [float(row["volume_shares"]) for row in bars]
    amounts = [float(row["amount_estimated"]) for row in bars]
    current = closes[-1]
    previous = closes[-2]
    volume_base = sum(volumes[-7:-1]) / max(1, len(volumes[-7:-1]))
    total_volume = sum(volumes)
    vwap = sum(amounts) / total_volume if total_volume else current
    prior_high = max(highs[-13:-1]) if len(highs) > 1 else current
    day_high = max(highs)
    return {
        "return_5m_pct": (current / previous - 1) * 100 if previous else 0,
        "volume_ratio_5m": volumes[-1] / volume_base if volume_base else 0,
        "vwap": vwap,
        "vwap_deviation_pct": (current / vwap - 1) * 100 if vwap else 0,
        "breakout_pct": (current / prior_high - 1) * 100 if prior_high else 0,
        "drawdown_from_high_pct": (current / day_high - 1) * 100 if day_high else 0,
        "current": current,
    }


def evaluate_state(
    bars: list[dict[str, Any]], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    if len(bars) < 6:
        raise ValueError("分时状态机至少需要6根5分钟K线")
    previous = previous or {}
    if previous.get("bar_time") and str(previous["bar_time"])[:10] != str(bars[-1]["time"])[:10]:
        previous = {}
    old = str(previous.get("state") or "dormant")
    held = int(previous.get("bars_in_state") or 0)
    m = _metrics(bars)
    if previous.get("bar_time") == bars[-1]["time"]:
        return {
            "state": old,
            "state_label": previous.get("state_label") or LABELS[old],
            "previous_state": previous.get("previous_state"),
            "entered_at": previous.get("entered_at") or bars[-1]["time"],
            "bars_in_state": held,
            "reason": previous.get("reason") or "状态延续",
            "changed": False,
            "bar_time": bars[-1]["time"],
            **{key: round(value, 4) for key, value in m.items()},
        }
    ret, vr, dev, breakout, drawdown = (
        m["return_5m_pct"], m["volume_ratio_5m"], m["vwap_deviation_pct"],
        m["breakout_pct"], m["drawdown_from_high_pct"],
    )
    next_state, reason = old, "状态延续"

    # 风险状态优先；确认后至少保持一根，减少阈值附近来回抖动。
    if drawdown <= -4.0 or (dev <= -2.0 and ret <= -1.0):
        next_state, reason = "failed", "跌破日内结构或VWAP"
    elif dev >= 5.0 or (vr >= 3.0 and ret >= 2.5):
        next_state, reason = "overheated", "价格偏离或单根放量过热"
    elif old in {"confirmation", "second_ignition"} and held >= 1 and -0.8 <= dev <= 1.0 and ret <= 0.3:
        next_state, reason = "pullback", "突破后缩量回踩VWAP"
    elif old == "pullback" and ret >= 0.45 and vr >= 1.25 and dev >= 0:
        next_state, reason = "second_ignition", "回踩后再次放量上行"
    elif old == "ignition" and held >= 1 and breakout >= 0 and dev >= 0.4 and vr >= 1.15:
        next_state, reason = "confirmation", "突破近期高点并站稳VWAP"
    elif ret >= 0.55 and vr >= 1.35 and dev >= 0.25:
        next_state, reason = "ignition", "5分钟量价同步启动"
    elif abs(dev) <= 1.2 and breakout >= -1.0 and 0.65 <= vr <= 1.8:
        next_state, reason = "setup", "价格靠近VWAP及短线突破位"
    elif old in {"failed", "overheated"} and held >= 3 and abs(dev) <= 1.2:
        next_state, reason = "setup", "风险释放后重新蓄势"
    elif old not in {"dormant", "failed", "overheated"} and held >= 2 and dev < -1.2:
        next_state, reason = "dormant", "启动条件消失"

    changed = next_state != old
    bar_time = bars[-1]["time"]
    entered_at = bar_time if changed else previous.get("entered_at", bar_time)
    return {
        "state": next_state,
        "state_label": LABELS[next_state],
        "previous_state": old if changed else previous.get("previous_state"),
        "entered_at": entered_at,
        "bars_in_state": 1 if changed else held + 1,
        "reason": reason if changed else previous.get("reason", reason),
        "changed": changed,
        "bar_time": bar_time,
        **{key: round(value, 4) for key, value in m.items()},
    }
