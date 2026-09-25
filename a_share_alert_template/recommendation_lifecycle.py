#!/usr/bin/env python3
"""推荐生命周期、可达性、MFE/MAE和事件记录。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from market_calendar import is_trade_date


OPEN_STATUSES = {"active", "entry_reached"}
OPPORTUNITY_MERGE_MINUTES = 45


def _signal_id(row: dict[str, Any]) -> str:
    identity = "|".join([
        str(row.get("symbol") or ""), str(row.get("recommendation_type") or ""),
        str(row.get("state_entered_at") or row.get("bar_time") or ""),
    ])
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]


def _opportunity_id(connection: sqlite3.Connection, row: dict[str, Any], now: datetime) -> str:
    """在同方向合并窗口内沿用机会；失效、过期或方向重置后创建新机会。"""
    existing = connection.execute(
        "SELECT opportunity_id FROM recommendation_lifecycle WHERE signal_id=?", (_signal_id(row),)
    ).fetchone()
    if existing and existing[0]:
        return str(existing[0])
    candidate = connection.execute("""
      SELECT opportunity_id,last_seen_at FROM recommendation_lifecycle
      WHERE symbol=? AND sector=? AND status IN ('active','entry_reached')
        AND opportunity_id IS NOT NULL
      ORDER BY last_seen_at DESC LIMIT 1
    """, (str(row.get("symbol") or ""), str(row.get("direction") or ""))).fetchone()
    if candidate:
        try:
            last_seen = datetime.fromisoformat(str(candidate["last_seen_at"] if isinstance(candidate, sqlite3.Row) else candidate[1]))
            if last_seen.tzinfo is not None:
                last_seen = last_seen.replace(tzinfo=None)
            compare_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
            if timedelta(0) <= compare_now - last_seen <= timedelta(minutes=OPPORTUNITY_MERGE_MINUTES):
                return str(candidate["opportunity_id"] if isinstance(candidate, sqlite3.Row) else candidate[0])
        except (TypeError, ValueError):
            pass
    identity = "|".join([
        str(row.get("symbol") or ""), str(row.get("direction") or ""),
        now.isoformat(timespec="seconds"), str(row.get("recommendation_type") or ""),
    ])
    return "opp_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]


def _next_trade_due(start: datetime, sessions: int) -> datetime:
    candidate: date = start.date()
    remaining = sessions
    while remaining:
        candidate += timedelta(days=1)
        if is_trade_date(candidate):
            remaining -= 1
    return datetime.combine(candidate, time(14, 55))


def _ensure_observations(connection: sqlite3.Connection, opportunity_id: str, signal_id: str,
                         symbol: str, entry_price: float, reached_at: datetime) -> str:
    due_times = {"T+1": _next_trade_due(reached_at, 1), "T+3": _next_trade_due(reached_at, 3)}
    now_text = reached_at.isoformat(timespec="seconds")
    for horizon, due_at in due_times.items():
        connection.execute("""
          INSERT OR IGNORE INTO recommendation_observations
          (opportunity_id,signal_id,symbol,horizon,due_at,entry_price,last_price,max_price,min_price,
           mfe_pct,mae_pct,target_hit,invalidated,completed_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (opportunity_id,signal_id,symbol,horizon,due_at.isoformat(timespec="seconds"),entry_price,
              entry_price,entry_price,entry_price,0.0,0.0,0,0,None,now_text))
    observation_until = max(due_times.values()).isoformat(timespec="seconds")
    connection.execute("""
      UPDATE recommendation_lifecycle
      SET opportunity_id=?,observation_until=?,entry_reference_price=COALESCE(entry_reference_price,?)
      WHERE opportunity_id=? OR signal_id=?
    """, (opportunity_id,observation_until,entry_price,opportunity_id,signal_id))
    return observation_until


def _record_evidence(connection: sqlite3.Connection, opportunity_id: str, signal_id: str,
                     row: dict[str, Any], captured_at: str) -> None:
    groups = (
        ("bull", "technical", "technical", row.get("support_evidence") or []),
        ("bear", "technical", "technical", row.get("opposing_evidence") or []),
        ("bull", "fundamental", "fundamental", row.get("fundamental_support_evidence") or []),
        ("bear", "fundamental", "fundamental", row.get("fundamental_opposing_evidence") or []),
        ("bull", "board", "board_strength", row.get("concept_evidence") or []),
        ("bear", "blocker", "recommendation_engine", row.get("blockers") or []),
    )
    symbol = str(row.get("symbol") or "")
    for side, evidence_group, source, values in groups:
        for value in dict.fromkeys(str(item).strip() for item in values if str(item).strip()):
            connection.execute("""
              INSERT OR IGNORE INTO recommendation_evidence
              (opportunity_id,captured_at,symbol,side,evidence_group,evidence_text,source)
              VALUES (?,?,?,?,?,?,?)
            """, (opportunity_id,captured_at,symbol,side,evidence_group,value,source))


def _update_observations(connection: sqlite3.Connection, current_by_symbol: dict[str, dict[str, Any]],
                         now: datetime) -> None:
    observations = connection.execute(
        "SELECT * FROM recommendation_observations WHERE completed_at IS NULL ORDER BY due_at"
    ).fetchall()
    for observation in observations:
        symbol = str(observation["symbol"])
        current = current_by_symbol.get(symbol) or {}
        price = float(current.get("price") or 0)
        if price <= 0:
            try:
                latest = connection.execute(
                    "SELECT price FROM stock_snapshots WHERE symbol=? ORDER BY captured_at DESC LIMIT 1", (symbol,)
                ).fetchone()
                price = float(latest[0] or 0) if latest else 0
            except sqlite3.OperationalError:
                price = 0
        if price <= 0:
            continue
        entry_price = float(observation["entry_price"] or price)
        max_price = max(float(observation["max_price"] or price), price)
        min_price = min(float(observation["min_price"] or price), price)
        levels = connection.execute("""
          SELECT MAX(target_price),MIN(NULLIF(invalid_price,0))
          FROM recommendation_lifecycle WHERE opportunity_id=?
        """, (observation["opportunity_id"],)).fetchone()
        target_price = float(levels[0] or 0)
        invalid_price = float(levels[1] or 0)
        due_at = datetime.fromisoformat(str(observation["due_at"]))
        compare_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
        completed_at = now.isoformat(timespec="seconds") if compare_now >= due_at else None
        connection.execute("""
          UPDATE recommendation_observations SET last_price=?,max_price=?,min_price=?,mfe_pct=?,mae_pct=?,
            target_hit=?,invalidated=?,completed_at=COALESCE(completed_at,?),updated_at=?
          WHERE opportunity_id=? AND horizon=?
        """, (
            price,max_price,min_price,round((max_price/entry_price-1)*100,3),round((min_price/entry_price-1)*100,3),
            int(bool(observation["target_hit"]) or (target_price > 0 and max_price >= target_price)),
            int(bool(observation["invalidated"]) or (invalid_price > 0 and min_price <= invalid_price)),
            completed_at,now.isoformat(timespec="seconds"),observation["opportunity_id"],observation["horizon"],
        ))


def lifecycle_context(connection: sqlite3.Connection, trade_date: str, holding_symbols: set[str] | None = None) -> dict[str, Any]:
    active = connection.execute(
        "SELECT symbol,sector FROM recommendation_lifecycle WHERE status IN ('active','entry_reached')"
    ).fetchall()
    sector_counts: dict[str, int] = {}
    for row in active:
        sector = row["sector"] if isinstance(row, sqlite3.Row) else row[1]
        sector_counts[str(sector)] = sector_counts.get(str(sector), 0) + 1
    return {
        "active_symbols": [str(row["symbol"] if isinstance(row, sqlite3.Row) else row[0]) for row in active],
        "active_sector_counts": sector_counts,
        "daily_new_count": connection.execute(
            "SELECT COUNT(*) FROM recommendation_lifecycle WHERE substr(first_seen_at,1,10)=?", (trade_date,)
        ).fetchone()[0],
        "holding_symbols": sorted(holding_symbols or set()),
    }


def _event(connection: sqlite3.Connection, signal_id: str, event_at: str, event_type: str, price: float, detail: dict[str, Any] | None = None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO recommendation_events VALUES (?,?,?,?,?)",
        (signal_id, event_at, event_type, price, json.dumps(detail or {}, ensure_ascii=False)),
    )


def update_lifecycle(connection: sqlite3.Connection, rows: list[dict[str, Any]], now: datetime, mode: str = "shadow", engine_version: str = "v7.1") -> list[dict[str, Any]]:
    now_text = now.isoformat(timespec="seconds")
    current_by_symbol = {str(row.get("symbol")): row for row in rows}
    with connection:
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if row.get("recommendation_status") != "recommend" or not symbol:
                continue
            signal_id = _signal_id(row)
            row["signal_id"] = signal_id
            existing = connection.execute("SELECT * FROM recommendation_lifecycle WHERE signal_id=?", (signal_id,)).fetchone()
            opportunity_id = _opportunity_id(connection, row, now)
            row["opportunity_id"] = opportunity_id
            price = float(row.get("price") or 0)
            entry_low = float(row.get("entry_low") or 0)
            entry_high = float(row.get("entry_high") or 0)
            invalid_price = float(row.get("invalid_price") or 0)
            target_price = float(row.get("target_price") or 0)
            status = "entry_reached" if entry_low <= price <= entry_high else "active"
            entry_reached_at = now_text if status == "entry_reached" else None
            if existing is None:
                connection.execute("""
                  INSERT INTO recommendation_lifecycle
                  (signal_id,symbol,name,sector,recommendation_type,state,role,status,first_seen_at,last_seen_at,
                   entry_low,entry_high,invalid_price,target_price,expires_at,entry_reached_at,invalidated_at,
                   target_hit_at,closed_at,max_price,min_price,mfe_pct,mae_pct,market_level,mode,engine_version,close_reason,
                   opportunity_id,observation_until,entry_reference_price)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    signal_id,symbol,row.get("name"),row.get("direction"),row.get("recommendation_type"),row.get("state"),row.get("role"),status,
                    now_text,now_text,entry_low,entry_high,invalid_price,target_price,row.get("expires_at"),entry_reached_at,None,None,None,
                    price,price,0.0,0.0,(row.get("market_level") or ""),mode,engine_version,None,
                    opportunity_id,None,price if entry_reached_at else None,
                ))
                _event(connection, signal_id, now_text, "created", price, {"status": status})
                _record_evidence(connection, opportunity_id, signal_id, row, now_text)
                if entry_reached_at:
                    _event(connection, signal_id, now_text, "entry_reached", price)
                    _ensure_observations(connection, opportunity_id, signal_id, symbol, price, now)
            elif existing["status"] in OPEN_STATUSES:
                if not existing["opportunity_id"]:
                    connection.execute(
                        "UPDATE recommendation_lifecycle SET opportunity_id=? WHERE signal_id=?",
                        (opportunity_id, signal_id),
                    )
                max_price = max(float(existing["max_price"] or price), price)
                min_price = min(float(existing["min_price"] or price), price)
                base = float(existing["entry_high"] or entry_high or price)
                mfe = (max_price / base - 1) * 100 if base else 0
                mae = (min_price / base - 1) * 100 if base else 0
                new_status = existing["status"]
                reached = existing["entry_reached_at"]
                if not reached and entry_low <= price <= entry_high:
                    new_status, reached = "entry_reached", now_text
                    _event(connection, signal_id, now_text, "entry_reached", price)
                    _ensure_observations(connection, opportunity_id, signal_id, symbol, price, now)
                if invalid_price > 0 and price <= invalid_price:
                    new_status = "invalidated"
                    _event(connection, signal_id, now_text, "invalidated", price)
                elif target_price > 0 and price >= target_price:
                    new_status = "target_hit"
                    _event(connection, signal_id, now_text, "target_hit", price)
                connection.execute("""
                  UPDATE recommendation_lifecycle SET last_seen_at=?,status=?,entry_reached_at=?,
                    invalidated_at=CASE WHEN ?='invalidated' THEN ? ELSE invalidated_at END,
                    target_hit_at=CASE WHEN ?='target_hit' THEN ? ELSE target_hit_at END,
                    closed_at=CASE WHEN ? NOT IN ('active','entry_reached') THEN ? ELSE closed_at END,
                    max_price=?,min_price=?,mfe_pct=?,mae_pct=? WHERE signal_id=?
                """, (now_text,new_status,reached,new_status,now_text,new_status,now_text,new_status,now_text,max_price,min_price,round(mfe,3),round(mae,3),signal_id))
            current = connection.execute("SELECT status FROM recommendation_lifecycle WHERE signal_id=?", (signal_id,)).fetchone()
            row["lifecycle_status"] = current[0] if current else status

        open_rows = connection.execute(
            "SELECT signal_id,symbol,status,expires_at FROM recommendation_lifecycle WHERE status IN ('active','entry_reached')"
        ).fetchall()
        for existing in open_rows:
            symbol = str(existing["symbol"])
            current = current_by_symbol.get(symbol)
            expires = datetime.fromisoformat(existing["expires_at"]) if existing["expires_at"] else None
            close_status = None
            reason = None
            price = float((current or {}).get("price") or 0)
            if expires and now > expires:
                close_status, reason = "expired", "超过信号有效期"
            elif current is not None and current.get("signal_id") and current.get("signal_id") != existing["signal_id"]:
                close_status, reason = "cancelled", "同标的出现新状态信号"
            elif current is not None and current.get("recommendation_status") != "recommend":
                close_status, reason = "cancelled", "推荐条件消失"
            if close_status:
                connection.execute(
                    "UPDATE recommendation_lifecycle SET status=?,closed_at=?,close_reason=?,last_seen_at=? WHERE signal_id=?",
                    (close_status,now_text,reason,now_text,existing["signal_id"]),
                )
                _event(connection, existing["signal_id"], now_text, close_status, price, {"reason": reason})
        for row in rows:
            if not row.get("signal_id"):
                continue
            current = connection.execute(
                "SELECT status FROM recommendation_lifecycle WHERE signal_id=?", (row["signal_id"],)
            ).fetchone()
            if current:
                row["lifecycle_status"] = current[0]
        _update_observations(connection, current_by_symbol, now)
    return rows
