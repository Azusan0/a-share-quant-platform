#!/usr/bin/env python3
"""正式监控链的推荐快照消费者；仅在配置 mode=live 时返回可执行结果。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from market_calendar import as_shanghai
from market_clock import is_fresh, is_trading_session, parse_time


def load_executable_recommendations(config: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    now = as_shanghai(now or datetime.now())
    settings = config.get("recommendation_engine", {}) if isinstance(config, dict) else {}
    if settings.get("mode", "shadow") != "live" or not is_trading_session(now):
        return []
    path = Path(settings.get("snapshot_file", "/root/.hermes/scripts/a_share_recommendation_snapshot.json"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not is_fresh(payload.get("generated_at"), now, int(settings.get("max_snapshot_age_minutes", 10))):
        return []
    rows = []
    for row in payload.get("recommendations") or []:
        expires = parse_time(row.get("expires_at"))
        if row.get("recommendation_status") != "recommend" or (expires and expires < now):
            continue
        if row.get("lifecycle_status") not in {"active", "entry_reached"}:
            continue
        rows.append(row)
    return rows


def to_monitor_alert(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    return {
        "time": now.isoformat(timespec="seconds"), "signal_type": "entry",
        "direction": row.get("direction"), "symbol": row.get("symbol"), "name": row.get("name"),
        "type": "stock", "strategy": "recommendation_engine", "strategy_label": row.get("recommendation_type"),
        "score": row.get("recommendation_score"), "price": row.get("price"),
        "entry_reasons": row.get("reasons") or [], "stop_loss": row.get("invalid_price"),
        "take_profit": row.get("target_price"), "entry_low": row.get("entry_low"),
        "entry_high": row.get("entry_high"), "max_chase_price": row.get("max_chase_price"),
        "risk_reward": row.get("risk_reward"), "signal_id": row.get("signal_id"),
        "message": f"推荐引擎：{row.get('direction')} / {row.get('name')}，{row.get('recommendation_type')}，风险收益比{row.get('risk_reward')}",
    }
