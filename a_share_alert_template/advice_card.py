#!/usr/bin/env python3
"""把推荐引擎、账户建议和持仓重组为前端实时操作卡。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from portfolio_store import PortfolioStore
from position_sizing import suggest_position_size


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[11:16] if len(text) >= 16 else ""


def _latest_state_lookup(snapshot_db: Path) -> dict[str, dict[str, Any]]:
    if not snapshot_db.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{snapshot_db}?mode=ro&immutable=1", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT symbol,state,sector,metrics_json FROM intraday_states").fetchall()
        connection.close()
    except sqlite3.Error:
        return {}
    import json

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        try:
            item.update(json.loads(item.pop("metrics_json")))
        except Exception:
            item.pop("metrics_json", None)
        result[str(item.get("symbol"))] = item
    return result


def _account_context(portfolio_db: Path, price_lookup: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    accounts: list[dict[str, Any]] = []
    positions: dict[str, dict[str, Any]] = {}
    advice: dict[str, dict[str, Any]] = {}
    try:
        with PortfolioStore(portfolio_db) as store:
            accounts = store.list_accounts()
            for account in accounts:
                portfolio = store.portfolio(account["account_id"], price_lookup)
                for position in portfolio["positions"]:
                    positions.setdefault(str(position["symbol"]), {**position, "account": account, "summary": portfolio["summary"]})
                for row in store.latest_advice(account["account_id"], limit=120):
                    symbol = str(row.get("symbol") or "")
                    if symbol and symbol not in advice:
                        advice[symbol] = {**row, "account": account}
    except Exception:
        pass
    return accounts, positions, advice


def build_signals(
    *,
    recommendations: dict[str, Any],
    portfolio_db: str | Path,
    snapshot_db: str | Path,
    price_lookup: dict[str, float] | None = None,
    limit: int = 16,
) -> list[dict[str, Any]]:
    """生成前端操作提示卡。仅重组现有 shadow/账户输出，不自动触发交易。"""
    price_lookup = price_lookup or {}
    portfolio_path = Path(portfolio_db)
    state_lookup = _latest_state_lookup(Path(snapshot_db))
    accounts, positions, advice = _account_context(portfolio_path, price_lookup)
    default_account = accounts[0] if accounts else {"risk_profile": "balanced", "max_position_pct": 20}
    default_summary = {"total_assets": 0, "cash": 0}
    if accounts:
        try:
            with PortfolioStore(portfolio_path) as store:
                default_summary = store.portfolio(default_account["account_id"], price_lookup)["summary"]
        except Exception:
            pass

    cards: list[dict[str, Any]] = []
    if not recommendations.get("snapshot_stale"):
        for row in recommendations.get("recommendations") or []:
            if row.get("recommendation_status") != "recommend":
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            current_value = _num((positions.get(symbol) or {}).get("market_value"))
            sizing = suggest_position_size(
                entry_price=row.get("entry_high") or row.get("price"),
                invalid_price=row.get("invalid_price"),
                total_assets=default_summary.get("total_assets"),
                cash=default_summary.get("cash"),
                risk_profile=str(default_account.get("risk_profile") or "balanced"),
                max_position_pct=default_account.get("max_position_pct", 20),
                current_symbol_value=current_value,
            )
            card = {
                "action": "buy" if symbol not in positions else "add",
                "symbol": symbol,
                "name": row.get("name") or symbol,
                "sector": row.get("direction"),
                "type": row.get("status_label") or row.get("recommendation_type"),
                "role": row.get("role"),
                "ts": _ts(row.get("generated_at") or recommendations.get("generated_at")),
                "price": row.get("price"),
                "entry_low": row.get("entry_low"),
                "entry_high": row.get("entry_high"),
                "max_chase": row.get("max_chase_price"),
                "invalid": row.get("invalid_price"),
                "target": row.get("target_price"),
                "rr": row.get("risk_reward"),
                "pos_pct": sizing.get("position_pct"),
                "shares": sizing.get("shares"),
                "max_loss": sizing.get("max_loss_if_invalid"),
                "confidence": min(95, max(35, _num(row.get("recommendation_score"), 50))),
                "scenario": row.get("market_level") or "影子推荐",
                "reasons": " · ".join([str(x) for x in (row.get("reasons") or [])[:4]]) or "推荐引擎给出可执行区间",
            }
            cards.append(card)

    label = {"hold": "继续持有", "protect": "提高警戒", "trim": "趋势转弱", "exit": "硬风险/止损", "watch": "观察计划"}
    for symbol, row in advice.items():
        action = str(row.get("action") or "")
        if action not in {"hold", "protect", "trim", "exit", "watch"}:
            continue
        position = positions.get(symbol) or {}
        state = state_lookup.get(symbol) or {}
        mapped = "trim" if action == "protect" else action
        if action == "watch" and symbol in positions:
            mapped = "hold"
        elif action == "watch":
            mapped = "entry_zone"
        quantity = int(position.get("quantity") or 0)
        ratio = _num(row.get("action_ratio_pct"))
        card = {
            "action": mapped,
            "symbol": symbol,
            "name": row.get("name") or position.get("name") or symbol,
            "sector": state.get("sector") or row.get("sector"),
            "type": label.get(action, action),
            "role": "持仓",
            "ts": _ts(row.get("generated_at")),
            "price": position.get("current_price") or state.get("current"),
            "cost": position.get("average_cost"),
            "invalid": row.get("invalid_price") or position.get("stop_price"),
            "target": row.get("target_price") or position.get("target_price"),
            "pnl_pct": position.get("pnl_pct"),
            "trend": row.get("trend_score"),
            "tolerance": row.get("tolerance_pct"),
            "ratio": ratio,
            "sell_shares": int(quantity * ratio / 100 // 100 * 100) if quantity and ratio else None,
            "confidence": row.get("confidence"),
            "scenario": row.get("trend_band") or row.get("phase"),
            "hard": action == "exit",
            "reasons": " · ".join([str(x) for x in (row.get("reasons") or [])[:4]]) or "账户监控生成动态建议",
        }
        cards.append(card)

    priority = {"exit": 0, "trim": 1, "entry_zone": 2, "buy": 3, "add": 4, "hold": 5}
    cards.sort(key=lambda x: (priority.get(str(x.get("action")), 9), str(x.get("ts") or "")))
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for card in cards:
        key = (str(card.get("action")), str(card.get("symbol")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    return unique[:limit]
