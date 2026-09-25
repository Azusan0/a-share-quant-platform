#!/usr/bin/env python3
"""可信复盘：机会、固定观察期、证据覆盖和人工成交偏差，不输出未校准胜率。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _trade_alignment(connection: sqlite3.Connection, portfolio_db: str | Path | None) -> dict[str, Any]:
    empty = {
        "eligible_opportunities": 0, "matched_opportunities": 0, "coverage_pct": 0,
        "average_delay_minutes": None, "average_slippage_pct": None,
    }
    if not portfolio_db or not Path(portfolio_db).exists() or "opportunity_id" not in _columns(connection, "recommendation_lifecycle"):
        return empty
    opportunities = connection.execute("""
      SELECT opportunity_id,symbol,MIN(first_seen_at) first_seen_at,
             MAX(observation_until) observation_until,AVG(entry_reference_price) entry_price
      FROM recommendation_lifecycle
      WHERE opportunity_id IS NOT NULL AND entry_reached_at IS NOT NULL
      GROUP BY opportunity_id,symbol
    """).fetchall()
    if not opportunities:
        return empty
    trades = sqlite3.connect(f"file:{Path(portfolio_db)}?mode=ro&immutable=1", uri=True, timeout=3)
    trades.row_factory = sqlite3.Row
    delays: list[float] = []
    slippages: list[float] = []
    matched = 0
    try:
        for item in opportunities:
            start = datetime.fromisoformat(str(item["first_seen_at"]))
            end = datetime.fromisoformat(str(item["observation_until"])) if item["observation_until"] else start + timedelta(days=7)
            trade = trades.execute("""
              SELECT traded_at,price_units FROM trades
              WHERE symbol=? AND side='buy' AND traded_at>=? AND traded_at<=?
              ORDER BY traded_at LIMIT 1
            """, (item["symbol"], start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone()
            if not trade:
                continue
            matched += 1
            traded_at = datetime.fromisoformat(str(trade["traded_at"]))
            delays.append(max(0.0, (traded_at - start).total_seconds() / 60))
            entry_price = float(item["entry_price"] or 0)
            if entry_price > 0:
                traded_price = float(trade["price_units"] or 0) / 10000
                slippages.append((traded_price / entry_price - 1) * 100)
    finally:
        trades.close()
    return {
        "eligible_opportunities": len(opportunities),
        "matched_opportunities": matched,
        "coverage_pct": round(matched / len(opportunities) * 100, 2),
        "average_delay_minutes": _average(delays),
        "average_slippage_pct": _average(slippages),
    }


def _shadow_suggestions(observation_summary: dict[str, dict[str, Any]], minimum_samples: int) -> list[dict[str, Any]]:
    t3 = observation_summary.get("T+3") or {}
    samples = int(t3.get("completed_samples") or 0)
    if samples < minimum_samples:
        return []
    target_rate = float(t3.get("target_touch_pct") or 0)
    invalid_rate = float(t3.get("invalid_touch_pct") or 0)
    suggestion = {
        "mode": "shadow",
        "max_single_change_pct": 5,
        "status": "需要滚动样本外验证与人工批准",
        "samples": samples,
    }
    if invalid_rate > target_rate:
        suggestion.update(
            parameter="recommendation_engine.entry_threshold",
            direction="提高",
            basis=f"T+3失效触达{invalid_rate:.1f}%高于目标触达{target_rate:.1f}%",
        )
    else:
        suggestion.update(
            parameter="recommendation_engine.exit_parameters",
            direction="仅复核，不自动调整",
            basis=f"T+3目标触达{target_rate:.1f}%，失效触达{invalid_rate:.1f}%",
        )
    return [suggestion]


def build_review(db_path: str | Path, minimum_samples: int = 30,
                 portfolio_db: str | Path | None = None) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"samples": 0, "coverage_pct": 0, "calibration_eligible": False, "reason": "快照数据库不存在"}
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    columns = _columns(connection, "recommendation_lifecycle")
    opportunity_expr = "COUNT(DISTINCT COALESCE(opportunity_id,'legacy_' || signal_id))" if "opportunity_id" in columns else "COUNT(*)"
    rows = connection.execute("""SELECT status,entry_reached_at,mfe_pct,mae_pct,close_reason
      FROM recommendation_lifecycle ORDER BY first_seen_at DESC LIMIT 1000""").fetchall()
    total = len(rows)
    opportunities = int(connection.execute(f"SELECT {opportunity_expr} FROM recommendation_lifecycle").fetchone()[0] or 0)
    reached = [row for row in rows if row["entry_reached_at"]]
    mfe = [float(row["mfe_pct"]) for row in reached if row["mfe_pct"] is not None]
    mae = [float(row["mae_pct"]) for row in reached if row["mae_pct"] is not None]
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[str(row["status"])] = statuses.get(str(row["status"]), 0) + 1

    observation_summary: dict[str, dict[str, Any]] = {}
    if _table_exists(connection, "recommendation_observations"):
        for horizon in ("T+1", "T+3"):
            observations = connection.execute(
                "SELECT completed_at,mfe_pct,mae_pct,target_hit,invalidated FROM recommendation_observations WHERE horizon=?",
                (horizon,),
            ).fetchall()
            completed = [item for item in observations if item["completed_at"]]
            completed_mfe = [float(item["mfe_pct"]) for item in completed if item["mfe_pct"] is not None]
            completed_mae = [float(item["mae_pct"]) for item in completed if item["mae_pct"] is not None]
            observation_summary[horizon] = {
                "samples": len(observations),
                "completed_samples": len(completed),
                "coverage_pct": round(len(completed) / len(observations) * 100, 2) if observations else 0,
                "average_mfe_pct": _average(completed_mfe),
                "average_mae_pct": _average(completed_mae),
                "target_touch_pct": round(sum(bool(item["target_hit"]) for item in completed) / len(completed) * 100, 2) if completed else 0,
                "invalid_touch_pct": round(sum(bool(item["invalidated"]) for item in completed) / len(completed) * 100, 2) if completed else 0,
            }
    evidence_entries = connection.execute("SELECT COUNT(*) FROM recommendation_evidence").fetchone()[0] if _table_exists(connection, "recommendation_evidence") else 0
    trade_alignment = _trade_alignment(connection, portfolio_db)
    suggestions = _shadow_suggestions(observation_summary, minimum_samples)
    connection.close()
    eligible = bool(suggestions)
    return {
        "samples": total,
        "opportunities": opportunities,
        "entry_reached_samples": len(reached),
        "coverage_pct": round(len(reached) / total * 100, 2) if total else 0,
        "average_mfe_pct": _average(mfe),
        "average_mae_pct": _average(mae),
        "status_counts": statuses,
        "fixed_observations": observation_summary,
        "evidence_ledger_entries": int(evidence_entries),
        "trade_alignment": trade_alignment,
        "shadow_calibration_suggestions": suggestions,
        "calibration_eligible": eligible,
        "minimum_samples": minimum_samples,
        "reason": "满足最小固定观察样本，仅生成shadow建议，仍需样本外验证和人工批准" if eligible else "固定观察样本不足，不展示胜率或自动调参",
    }
