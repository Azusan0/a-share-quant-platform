#!/usr/bin/env python3
"""账户级仓位预算，只给人工决策上限，不产生交易。"""
from __future__ import annotations

import math
from typing import Any


RISK_BUDGET_PCT = {"conservative": 0.5, "balanced": 0.8, "aggressive": 1.0}


def suggest_position_size(
    *,
    entry_price: Any,
    invalid_price: Any,
    total_assets: Any,
    cash: Any,
    risk_profile: str = "balanced",
    max_position_pct: Any = 20,
    current_symbol_value: Any = 0,
    sector_remaining_value: Any | None = None,
    atr_pct: Any | None = None,
    lot: int = 100,
    fee_buffer_pct: float = 0.2,
) -> dict[str, Any]:
    try:
        entry = float(entry_price)
        invalid = float(invalid_price)
        assets = max(0.0, float(total_assets))
        available_cash = max(0.0, float(cash))
        current_value = max(0.0, float(current_symbol_value))
        position_limit_pct = max(0.0, float(max_position_pct))
        atr = max(0.0, float(atr_pct or 0))
    except (TypeError, ValueError):
        return {"actionable": False, "reason": "仓位计算参数无效", "shares": 0}
    if not (entry > invalid > 0) or assets <= 0 or available_cash <= 0 or lot <= 0:
        return {"actionable": False, "reason": "缺少有效入场价、失效价或可用资金", "shares": 0}

    risk_pct = RISK_BUDGET_PCT.get(risk_profile, RISK_BUDGET_PCT["balanced"])
    effective_gap = max(entry - invalid, entry * 0.02, entry * atr / 100 * 1.2)
    risk_budget = assets * risk_pct / 100
    by_risk = math.floor(risk_budget / effective_gap / lot) * lot
    symbol_capacity = max(0.0, assets * position_limit_pct / 100 - current_value)
    by_symbol = math.floor(symbol_capacity / entry / lot) * lot
    spendable = available_cash / (1 + max(0.0, fee_buffer_pct) / 100)
    by_cash = math.floor(spendable / entry / lot) * lot
    candidates = [by_risk, by_symbol, by_cash]
    if sector_remaining_value is not None:
        try:
            candidates.append(math.floor(max(0.0, float(sector_remaining_value)) / entry / lot) * lot)
        except (TypeError, ValueError):
            return {"actionable": False, "reason": "板块剩余额度无效", "shares": 0}
    shares = max(0, min(candidates))
    if shares < lot:
        return {"actionable": False, "reason": "账户剩余风险额度不足100股", "shares": 0,
                "risk_budget_pct": risk_pct, "effective_stop_gap": round(effective_gap, 4)}
    position_value = shares * entry
    return {
        "actionable": True,
        "shares": int(shares),
        "position_value": round(position_value, 2),
        "position_pct": round(position_value / assets * 100, 2),
        "max_loss_if_invalid": round(shares * effective_gap, 2),
        "risk_budget_pct": risk_pct,
        "effective_stop_gap": round(effective_gap, 4),
        "note": "仅为账户风险上限，不代表自动买入",
    }
