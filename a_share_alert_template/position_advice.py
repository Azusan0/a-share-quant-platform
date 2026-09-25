#!/usr/bin/env python3
"""多账户持仓动态防卖飞建议状态机。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any


STATE_SCORE = {"second_ignition": 95, "confirmation": 85, "ignition": 75, "setup": 60,
               "pullback": 58, "dormant": 40, "overheated": 45, "failed": 20}


def _num(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_position(position: dict[str, Any], snapshot: dict[str, Any], evidence: dict[str, Any],
                      risk_profile: str = "balanced", previous: dict[str, Any] | None = None,
                      now: datetime | None = None, phase: str = "intraday") -> dict[str, Any]:
    now, previous = now or datetime.now(), previous or {}
    price = _num(snapshot.get("price"), _num(position.get("current_price"), _num(position.get("average_cost"))))
    cost = max(_num(position.get("average_cost")), .0001)
    high = max(_num(position.get("highest_price"), price), _num(snapshot.get("high"), price), price)
    pnl_pct = (price / cost - 1) * 100
    drawdown_pct = (price / high - 1) * 100 if high else 0
    market = max(0, min(100, _num(evidence.get("market_score"), 50)))
    sector = max(0, min(100, _num(evidence.get("sector_score"), 50)))
    vwap_dev = _num(evidence.get("vwap_deviation_pct"))
    relative = _num(evidence.get("relative_strength_pct"))
    structure = max(0, min(100, 55 + vwap_dev * 8 + relative * 6))
    state_score = STATE_SCORE.get(str(evidence.get("intraday_state") or "dormant"), 40)
    profit_cushion = max(0, min(100, 45 + pnl_pct * 5))
    hard_risks = list(evidence.get("hard_risks") or [])
    event_penalty = min(40, len(hard_risks) * 20)
    score = market * .25 + sector * .25 + structure * .25 + state_score * .15 + profit_cushion * .10 - event_penalty
    global_affected = bool(evidence.get("global_affected"))
    global_score = max(0, min(100, _num(evidence.get("global_sector_score"), 50)))
    global_confidence = max(0, min(100, _num(evidence.get("global_confidence"))))
    if global_affected and global_confidence >= 50:
        score += (global_score - 50) * .08
    score = max(0, min(100, score))
    old_band = previous.get("trend_band")
    if old_band == "strong" and score >= 65:
        band = "strong"
    elif old_band == "weak" and score <= 50:
        band = "weak"
    else:
        band = "strong" if score >= 70 else "weak" if score < 45 else "neutral"
    tolerance = {"strong": 4.5, "neutral": 2.75, "weak": 1.5}[band]
    tolerance += {"conservative": -.5, "balanced": 0, "aggressive": .5}.get(risk_profile, 0)
    tolerance = max(1, min(5, tolerance))
    stop = _num(position.get("stop_price"), cost * .95)
    below_stop = price > 0 and price <= stop
    groups: dict[str, str] = {}
    if int(evidence.get("below_vwap_bars") or 0) >= 2:
        groups["price_risk"] = "连续两根1分钟K低于VWAP"
    if evidence.get("rebound_failed") is True:
        groups.setdefault("price_risk", "反抽未收回关键位")
    if drawdown_pct <= -tolerance:
        groups.setdefault("price_risk", f"高点回撤{abs(drawdown_pct):.2f}%超过动态容忍{tolerance:.2f}%")
    if evidence.get("volume_down") is True:
        groups["volume"] = "放量下跌"
    if evidence.get("sector_weak"):
        groups["sector"] = "所属板块同步转弱"
    if int(evidence.get("risk_persist_bars") or 0) >= 3:
        groups["persistence"] = "风险状态持续3根以上"
    modifier_reasons = []
    if global_affected and global_score <= 30 and global_confidence >= 60 and groups:
        modifier_reasons.append("美日韩映射因子同步偏弱")
    confirmations = list(groups.values()) + modifier_reasons
    independent_count = len(groups)
    has_internal_risk = "price_risk" in groups
    reasons = [f"趋势{band}({score:.1f})", f"浮盈{pnl_pct:.2f}%", f"高点回撤{drawdown_pct:.2f}%"]
    if global_affected:
        reasons.append(f"美日韩映射因子{global_score:.1f}分")
    factor_scope = "global" if global_affected and global_confidence >= 50 else "domestic"
    domestic_drivers = list(evidence.get("domestic_drivers") or [])
    if not domestic_drivers:
        if evidence.get("sector_name"):
            domestic_drivers.append(f"国内板块：{evidence['sector_name']}")
        domestic_drivers.extend(hard_risks[:2])
    global_drivers = list(evidence.get("global_drivers") or [])
    if factor_scope == "domestic" and domestic_drivers:
        reasons.append("国内依据：" + "、".join(domestic_drivers[:2]))
    elif factor_scope == "global" and global_drivers:
        reasons.append("外盘依据：" + "、".join(global_drivers[:2]))
    if hard_risks or below_stop:
        action, ratio = "exit", 100
        reasons.append("硬风险:" + "、".join(hard_risks) if hard_risks else f"跌破硬止损{stop:.3f}")
    elif has_internal_risk and independent_count >= 2 and band == "weak":
        action, ratio = "trim", 50
        reasons.extend(confirmations[:3])
    elif has_internal_risk and independent_count >= 2 and band == "neutral":
        action, ratio = "trim", 25
        reasons.extend(confirmations[:3])
    elif confirmations:
        action, ratio = "protect", 0
        reasons.extend(confirmations[:2])
    else:
        action, ratio = "hold", 0
    quantity = max(0, int(_num(position.get("quantity"))))
    trim_shares = min(quantity, max(0, int(quantity * ratio / 100 / 100) * 100)) if ratio else 0
    add_ratio = 10 if band == "strong" and not confirmations and not hard_risks else 0
    add_shares = min(quantity, max(0, int(quantity * add_ratio / 100 / 100) * 100)) if add_ratio else 0
    watch_price = round(max(stop, price * (1 - tolerance / 100)), 4) if price else round(stop, 4)
    if action == "exit":
        operation = f"现在清仓/止损，约{quantity}股；触发硬止损{stop:.3f}。"
    elif action == "trim":
        operation = f"现在减持{ratio:.0f}%（约{trim_shares or '不足1手'}股）；剩余仓位跌破{watch_price:.3f}再继续减。"
    elif action == "protect":
        operation = f"继续持有但停止加仓；若跌破{watch_price:.3f}，减持25%（约{max(0, int(quantity * .25 / 100) * 100) or '不足1手'}股）。"
    elif add_ratio:
        operation = f"趋势偏强，回踩不破支撑可增持约{add_ratio}%（约{add_shares or '不足1手'}股），不追高。"
    else:
        operation = f"继续观察并持有，不加仓；跌破{watch_price:.3f}再执行减仓。"
    confidence = min(95, 45 + 10 * independent_count + (5 if modifier_reasons else 0) + (15 if hard_risks or below_stop else 0))
    return {
        "advice_id": uuid.uuid4().hex, "account_id": position["account_id"], "symbol": position["symbol"],
        "name": position.get("name") or position["symbol"], "phase": phase, "action": action,
        "action_ratio_pct": ratio, "trend_score": round(score, 2), "trend_band": band,
        "confidence": confidence, "tolerance_pct": round(tolerance, 2), "price": round(price, 4),
        "pnl_pct": round(pnl_pct, 2), "drawdown_pct": round(drawdown_pct, 2), "hard_stop_price": round(stop, 4),
        "global_sector_score": round(global_score, 1) if global_affected else None,
        "factor_scope": factor_scope, "domestic_drivers": domestic_drivers[:5], "global_drivers": global_drivers[:5],
        "operation_instruction": operation, "next_action_price": watch_price,
        "add_ratio_pct": add_ratio, "add_shares": add_shares, "trim_shares": trim_shares,
        "reasons": reasons, "trigger_conditions": confirmations,
        "evidence_groups": groups,
        "invalidation": ["价格收回VWAP并保持两根1分钟K", "板块强度恢复", "建议超过有效期"],
        "generated_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=5 if phase == "intraday" else 30)).isoformat(timespec="seconds"),
    }
