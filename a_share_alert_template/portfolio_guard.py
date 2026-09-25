#!/usr/bin/env python3
"""推荐组合约束：控制推荐数量、板块集中和重复标的。"""
from __future__ import annotations

from typing import Any


def _downgrade(row: dict[str, Any], reason: str) -> dict[str, Any]:
    item = dict(row)
    item["recommendation_status"] = "watch"
    item["status_label"] = "观察"
    item.setdefault("blockers", []).append(f"组合约束：{reason}")
    return item


def apply_portfolio_guard(
    rows: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    market_level: str = "neutral",
) -> list[dict[str, Any]]:
    config = config or {}
    context = context or {}
    max_by_market = {
        "risk_on": int(config.get("risk_on_max_active", 3)),
        "positive": int(config.get("positive_max_active", 2)),
        "neutral": int(config.get("neutral_max_active", 1)),
        "weak": int(config.get("weak_max_active", 0)),
        "risk_off": int(config.get("risk_off_max_active", 0)),
    }
    max_active = min(int(config.get("max_active_total", 3)), max_by_market.get(market_level, 1))
    max_per_sector = int(config.get("max_active_per_sector", 1))
    max_new_daily = int(config.get("max_new_per_day", 5))
    min_score = float(config.get("min_recommendation_score", 50))
    active_symbols = set(context.get("active_symbols") or [])
    holding_symbols = set(context.get("holding_symbols") or [])
    daily_new = int(context.get("daily_new_count") or 0)
    accepted_symbols: set[str] = set(active_symbols)
    sector_counts: dict[str, int] = dict(context.get("active_sector_counts") or {})

    candidates = sorted(
        rows,
        key=lambda row: (row.get("recommendation_status") == "recommend", float(row.get("recommendation_score") or 0), float(row.get("risk_reward") or 0)),
        reverse=True,
    )
    guarded: list[dict[str, Any]] = []
    for row in candidates:
        if row.get("recommendation_status") != "recommend":
            guarded.append(row)
            continue
        symbol = str(row.get("symbol") or "")
        sector = str(row.get("direction") or "未知")
        is_existing = symbol in active_symbols
        if symbol in holding_symbols:
            guarded.append(_downgrade(row, "已在持仓或自选持仓中"))
        elif float(row.get("recommendation_score") or 0) < min_score:
            guarded.append(_downgrade(row, f"推荐分低于{min_score:.0f}"))
        elif row.get("role") == "leader" and row.get("state") not in {"confirmation", "second_ignition"}:
            guarded.append(_downgrade(row, "先锋股仅确认或二次启动可执行"))
        elif not is_existing and daily_new >= max_new_daily:
            guarded.append(_downgrade(row, "当日新推荐数量已达上限"))
        elif not is_existing and len(accepted_symbols) >= max_active:
            guarded.append(_downgrade(row, "组合活动推荐已达上限"))
        elif not is_existing and sector_counts.get(sector, 0) >= max_per_sector:
            guarded.append(_downgrade(row, "同板块活动推荐已达上限"))
        else:
            guarded.append(row)
            accepted_symbols.add(symbol)
            if not is_existing:
                daily_new += 1
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
    guarded.sort(key=lambda row: (row.get("recommendation_status") == "recommend", float(row.get("recommendation_score") or 0)), reverse=True)
    return guarded

