#!/usr/bin/env python3
"""根据P2证据生成收盘后、盘前和竞价三情景计划。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def build_plan(symbol: str, name: str, evidence: dict[str, Any], phase: str = "overnight", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    technical = float(evidence.get("technical_score") or 50)
    sector = float(evidence.get("sector_score") or 50)
    market = float(evidence.get("market_score") or 50)
    global_affected = bool(evidence.get("global_affected"))
    global_score = float(evidence.get("global_sector_score") or 50)
    global_confidence = float(evidence.get("global_confidence") or 0)
    auction_score = float((evidence.get("auction") or {}).get("score") or 0)
    hard = list(evidence.get("hard_risks") or [])
    if global_affected:
        # 外盘仅作为软因子，不覆盖本地技术面、板块强度和硬风险。
        global_weight = .15 if phase in {"overnight", "premarket"} else .1
        remaining = 1 - global_weight
        base = technical * (.4 * remaining) + sector * (.35 * remaining) + market * (.25 * remaining) + global_score * global_weight
    else:
        base = technical * .4 + sector * .35 + market * .25
    base += max(-20, min(20, auction_score)) - len(hard) * 20
    bull = max(10, min(70, 20 + (base - 40) * .8))
    bear = max(10, min(70, 20 + (55 - base) * .8 + len(hard) * 10))
    neutral = max(10, 100 - bull - bear)
    total = bull + neutral + bear
    probabilities = {"bullish": round(bull / total * 100), "neutral": round(neutral / total * 100), "bearish": 0}
    probabilities["bearish"] = 100 - probabilities["bullish"] - probabilities["neutral"]
    support, resistance = evidence.get("support_price"), evidence.get("resistance_price")
    confidence = min(90, 45 + int(bool(support)) * 10 + int(bool(resistance)) * 10
                     + int(global_affected and global_confidence >= 60) * 5
                     - len(evidence.get("missing_data") or []) * 5)
    global_impact = str(evidence.get("global_impact") or "neutral")
    global_drivers = list(evidence.get("global_drivers") or [])
    strong_global = "且美日韩映射因子不转弱" if global_affected else ""
    weak_global = "、美日韩映射因子偏弱" if global_impact == "negative" else ""
    return {"symbol": symbol, "name": name, "phase": phase, "probabilities": probabilities,
            "bias": max(probabilities, key=probabilities.get), "confidence": max(10, confidence),
            "support_price": support, "resistance_price": resistance, "hard_risks": hard,
            "global_factor": {"affected": global_affected, "score": round(global_score, 1),
                              "impact": global_impact, "confidence": round(global_confidence),
                              "sectors": evidence.get("global_sectors") or [], "drivers": global_drivers},
            "scenarios": [
                {"name": "偏强", "trigger": f"板块保持强势且价格守住VWAP/支撑{strong_global}", "advice": "持有；回踩确认后再考虑计划仓位"},
                {"name": "中性", "trigger": "板块和个股无同步方向", "advice": "等待首个完整5分钟结构，不在竞价噪声中操作"},
                {"name": "偏弱", "trigger": f"低开、板块转弱{weak_global}且反抽失败", "advice": "按动态退出状态机分批减仓，硬风险优先"},
            ], "missing_data": evidence.get("missing_data") or [],
            "generated_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=16 if phase == "overnight" else 2)).isoformat(timespec="seconds")}
