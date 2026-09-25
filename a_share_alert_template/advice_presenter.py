#!/usr/bin/env python3
"""把引擎输出转换为网页/QQ共用的可解释建议。"""
from __future__ import annotations

from typing import Any


def confidence_band(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "未知"
    return "高" if score >= 75 else "中" if score >= 55 else "低"


def position_card(account_name: str, advice: dict[str, Any]) -> dict[str, Any]:
    labels = {"hold": "继续观察/持有", "protect": "持有但停止加仓", "trim": f"建议减仓{advice.get('action_ratio_pct') or 0}%",
              "exit": "建议清仓/止损"}
    hard = bool(advice.get("action") == "exit")
    return {
        "title": f"{account_name} · 持仓建议",
        "name": advice.get("name") or advice.get("symbol"),
        "symbol": advice.get("symbol"),
        "action": labels.get(str(advice.get("action")), str(advice.get("action"))),
        "price": advice.get("price"),
        "invalid_price": advice.get("hard_stop_price"),
        "next_action_price": advice.get("next_action_price"),
        "operation_instruction": advice.get("operation_instruction"),
        "add_ratio_pct": advice.get("add_ratio_pct") or 0,
        "add_shares": advice.get("add_shares") or 0,
        "trim_shares": advice.get("trim_shares") or 0,
        "factor_scope": advice.get("factor_scope") or "domestic",
        "factor_scope_label": "外盘美日韩" if advice.get("factor_scope") == "global" else "国内政策/消息",
        "confidence_band": confidence_band(advice.get("confidence")),
        "expires_at": advice.get("expires_at"),
        "reasons": list(advice.get("reasons") or [])[:4],
        "hard_risk": hard,
        "data_quality": advice.get("data_quality") or "partial",
    }


def plan_card(account_name: str, item: dict[str, Any], plan: dict[str, Any], sizing: dict[str, Any] | None = None) -> dict[str, Any]:
    probabilities = plan.get("probabilities") or {}
    bias = {"bullish": "偏强", "neutral": "中性", "bearish": "偏弱"}.get(plan.get("bias"), str(plan.get("bias") or "未知"))
    return {
        "title": f"{account_name} · {plan.get('phase') or '盘前'}计划",
        "name": item.get("name") or item.get("symbol"),
        "symbol": item.get("symbol"),
        "action": "观察计划" if not sizing or not sizing.get("actionable") else "可按风险预算分批",
        "bias": bias,
        "confidence_band": confidence_band(plan.get("confidence")),
        "entry_low": item.get("entry_low"),
        "entry_high": item.get("entry_high"),
        "invalid_price": item.get("invalid_price") or plan.get("support_price"),
        "support_price": plan.get("support_price"),
        "resistance_price": plan.get("resistance_price"),
        "scenario": probabilities,
        "sizing": sizing or {"actionable": False, "reason": "未计算账户仓位"},
        "expires_at": plan.get("expires_at"),
        "reasons": list(plan.get("reasons") or [])[:4],
        "data_quality": plan.get("data_quality") or "partial",
    }


def render_qq(card: dict[str, Any]) -> str:
    lines = [f"【{card.get('title')}】{card.get('name')}({card.get('symbol')})",
             f"动作：{card.get('action')}"]
    if card.get("bias"):
        lines.append(f"情景：{card.get('bias')} · 置信等级{card.get('confidence_band')}")
    elif card.get("confidence_band"):
        lines.append(f"置信等级：{card.get('confidence_band')}")
    if card.get("entry_low") is not None or card.get("entry_high") is not None:
        lines.append(f"参考区间：{card.get('entry_low') or '—'} - {card.get('entry_high') or '—'}")
    if card.get("price") is not None:
        lines.append(f"现价：{card.get('price')}")
    if card.get("invalid_price") is not None:
        lines.append(f"失效/止损锚：{card.get('invalid_price')}")
    if card.get("next_action_price") is not None:
        lines.append(f"下一触发价：{card.get('next_action_price')}")
    if card.get("operation_instruction"):
        lines.append(f"现在怎么做：{card.get('operation_instruction')}")
    if card.get("factor_scope_label"):
        lines.append(f"消息面口径：{card.get('factor_scope_label')}")
    sizing = card.get("sizing") or {}
    if sizing.get("actionable"):
        lines.append(f"账户上限：{sizing['shares']}股 / {sizing['position_pct']}%，失效最大计划亏损约{sizing['max_loss_if_invalid']}元")
    elif sizing:
        lines.append(f"仓位：不提供可执行数量（{sizing.get('reason') or '数据不足'}）")
    reasons = card.get("reasons") or []
    if reasons:
        lines.append("依据：" + "；".join(str(item) for item in reasons[:4]))
    if card.get("expires_at"):
        lines.append(f"有效至：{card.get('expires_at')}")
    lines.append(f"数据质量：{card.get('data_quality') or 'partial'}")
    lines.append("仅作辅助，不自动下单，请人工确认。")
    return "\n".join(lines)
