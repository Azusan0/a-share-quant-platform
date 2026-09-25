#!/usr/bin/env python3
"""A股候选推荐引擎（影子模式）。

只消费已归一化的动态池、影子候选和分时状态，不负责抓取行情，也不直接发消息。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from global_market_factor import factor_for_stock, load_snapshot
from market_clock import is_fresh, is_trading_session
from portfolio_guard import apply_portfolio_guard
from data_quality import recommendation_quality
from recommendation_lifecycle import lifecycle_context, update_lifecycle
from snapshot_store import SnapshotStore


STATE_MAP = {
    "watch": "setup",
    "starting": "ignition",
    "confirmed": "confirmation",
    "overheated": "overheated",
    "dormant": "dormant",
}
EXECUTABLE_STATES = {"ignition", "confirmation", "pullback", "second_ignition"}
STATUS_LABELS = {"recommend": "推荐", "watch": "观察", "reject": "拒绝"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _role(rows: list[dict[str, Any]], row: dict[str, Any]) -> str:
    """板块内按涨幅/流动性近似划分角色，避免总分前三全部变成追涨标的。"""
    ordered = sorted(rows, key=lambda x: (_num(x.get("change_pct")), _num(x.get("score"))), reverse=True)
    if row.get("type") == "etf":
        return "anchor"
    index = next((i for i, item in enumerate(ordered) if item.get("symbol") == row.get("symbol")), len(ordered))
    change = _num(row.get("change_pct"))
    if index == 0 and change >= 5:
        return "leader"
    if index <= max(1, len(ordered) // 3):
        return "core"
    return "catch_up"


def _state(row: dict[str, Any], state_rows: dict[str, dict[str, Any]]) -> str:
    current = state_rows.get(str(row.get("symbol")), {})
    if current.get("state"):
        return str(current["state"])
    return STATE_MAP.get(str(row.get("stage")), "dormant")


def build_recommendation(
    row: dict[str, Any],
    sector: dict[str, Any],
    role: str,
    state: str,
    now: datetime,
    *,
    risk_reward_min: float = 2.0,
    market_level: str = "neutral",
    max_state_age_minutes: int = 12,
    max_target_up_pct: float = 15.0,
) -> dict[str, Any]:
    price = _num(row.get("price"))
    vwap = _num(row.get("vwap"), price)
    day_low = _num(row.get("low"), price)
    day_high = _num(row.get("high"), price)
    change = _num(row.get("change_pct"))
    volume_ratio = _num(row.get("same_time_volume_ratio"), _num(row.get("volume_ratio_5m")))
    diagnosis = row.get("technical_diagnosis") if isinstance(row.get("technical_diagnosis"), dict) else {}
    fundamental = row.get("fundamental_diagnosis") if isinstance(row.get("fundamental_diagnosis"), dict) else {}
    board = row.get("board_strength") if isinstance(row.get("board_strength"), dict) else {}
    global_factor = row.get("global_factor") if isinstance(row.get("global_factor"), dict) else {}
    blockers: list[str] = []
    reasons: list[str] = []

    if not price or not vwap:
        blockers.append("价格或VWAP缺失")
    if not is_fresh(row.get("bar_time"), now, max_state_age_minutes):
        blockers.append("分时状态过期")
    if not row.get("volume_available", True) or volume_ratio <= 0:
        blockers.append("同期量能缺失")
    if state not in EXECUTABLE_STATES:
        blockers.append(f"分时状态{state}不可执行")
    if state == "overheated" or _num(row.get("rsi_live")) >= 76 or change >= 9:
        blockers.append("追涨或过热")
    if row.get("limit_up") or (change >= 9.5 and _num(row.get("close_position"), 0) >= 0.98):
        blockers.append("封死涨停仅作方向锚")
    if role == "anchor":
        blockers.append("锚点不作为可执行买入标的")
    if market_level in {"weak", "risk_off"} and state == "ignition":
        blockers.append("弱市不接受首次启动")
    if (global_factor.get("global_affected") and _num(global_factor.get("global_sector_score"), 50) <= 30
            and _num(global_factor.get("global_confidence")) >= 60 and state == "ignition"):
        blockers.append("美日韩映射因子显著偏弱，首次启动降为观察")
    quality = recommendation_quality(row, diagnosis, price=price, vwap=vwap, volume_ratio=volume_ratio, now=now)

    if state == "second_ignition":
        recommendation_type = "second_ignition"
    elif state == "pullback":
        recommendation_type = "pullback"
    elif state == "confirmation":
        recommendation_type = "confirmation"
    elif state == "ignition":
        recommendation_type = "first_ignition"
    elif role == "anchor":
        recommendation_type = "anchor"
    elif state == "overheated":
        recommendation_type = "overheated"
    else:
        recommendation_type = "setup"

    if state in {"confirmation", "second_ignition"}:
        entry_low = max(vwap, price * 0.995)
        entry_high = price * 1.005
    elif state in {"ignition", "pullback"}:
        entry_low = max(vwap * 0.995, day_low)
        entry_high = price * 1.003
    else:
        entry_low, entry_high = price, price
    invalid_price = min(vwap * 0.98, day_low * 0.995) if price else 0.0
    risk_per_share = max(entry_low - invalid_price, price * 0.005) if price else 0.0
    # 目标价来自可观察压力：日内高点、候选近期高点和至少一倍风险，不为凑阈值反推。
    resistance_candidates = [
        value for value in (
            day_high, _num(row.get("previous_high")), _num(row.get("high20")),
            _num(diagnosis.get("resistance_price")),
        )
        if entry_high < value <= price * (1 + max_target_up_pct / 100)
    ]
    fallback_target = min(entry_high + risk_per_share * 2.2, price * (1 + max_target_up_pct / 100)) if price else 0.0
    target_price = min(resistance_candidates) if resistance_candidates else fallback_target
    risk_reward = (target_price - entry_high) / risk_per_share if risk_per_share else 0.0
    max_chase_price = entry_high

    if risk_reward < risk_reward_min:
        blockers.append(f"风险收益比不足{risk_reward:.2f}")
    if not quality["actionable"] and "数据质量不足以执行" not in blockers:
        blockers.append("数据质量不足以执行")
    if blockers:
        status = "watch" if state in {"setup", "ignition", "pullback", "confirmation", "second_ignition"} else "reject"
    else:
        status = "recommend"

    if _num(sector.get("score")) >= 70:
        reasons.append(f"板块强度{_num(sector.get('score')):.1f}")
    if role == "catch_up":
        reasons.append("板块补涨角色，优先寻找早期位置")
    elif role == "core":
        reasons.append("板块中军，流动性与趋势较稳定")
    if volume_ratio >= 1.15:
        reasons.append(f"同期量比{volume_ratio:.2f}")
    if _num(row.get("breakout_prev_high_pct")) >= 0:
        reasons.append("突破近期高点")
    if diagnosis.get("support_evidence"):
        reasons.append(str(diagnosis["support_evidence"][0]))
    if fundamental.get("support_evidence"):
        reasons.append(str(fundamental["support_evidence"][0]))
    if board.get("concept_evidence"):
        reasons.append(str(board["concept_evidence"][0]))
    if global_factor.get("global_affected"):
        impact_label = {"positive": "偏多", "negative": "偏空", "neutral": "中性"}.get(str(global_factor.get("global_impact")), "中性")
        reasons.append(f"美日韩映射因子{impact_label}{_num(global_factor.get('global_sector_score'), 50):.1f}分")
        reasons.extend(list(global_factor.get("global_drivers") or [])[:2])
    reasons.append(f"分时状态{state}")

    global_adjust = (_num(global_factor.get("global_sector_score"), 50) - 50) * .12 if global_factor.get("global_affected") else 0
    recommendation_score = min(100, max(0, _num(sector.get("score")) * .3 + _num(row.get("score")) * .5 + min(volume_ratio, 2.5) * 8 + global_adjust))

    return {
        "symbol": row.get("symbol"), "name": row.get("name") or row.get("symbol"),
        "direction": row.get("direction") or sector.get("direction"),
        "recommendation_status": status, "status_label": STATUS_LABELS[status],
        "recommendation_type": recommendation_type, "state": state, "role": role,
        "bar_time": row.get("bar_time"), "state_entered_at": row.get("state_entered_at") or row.get("entered_at"),
        "sector_score": round(_num(sector.get("score")), 2),
        "candidate_score": round(_num(row.get("score")), 2),
        "recommendation_score": round(recommendation_score, 2),
        "price": round(price, 3), "vwap": round(vwap, 3),
        "entry_low": round(entry_low, 3), "entry_high": round(entry_high, 3),
        "max_chase_price": round(max_chase_price, 3), "invalid_price": round(invalid_price, 3),
        "target_price": round(target_price, 3), "risk_reward": round(risk_reward, 2),
        "volume_ratio_5m": round(volume_ratio, 2), "change_pct": round(change, 2),
        "data_quality": quality["overall"],
        "data_quality_detail": quality,
        "technical_score": diagnosis.get("technical_score"),
        "technical_trend": diagnosis.get("trend"),
        "technical_data_quality": diagnosis.get("data_quality", "missing"),
        "support_price": diagnosis.get("support_price"),
        "resistance_price": diagnosis.get("resistance_price"),
        "support_evidence": diagnosis.get("support_evidence") or [],
        "opposing_evidence": diagnosis.get("opposing_evidence") or [],
        "missing_data": list(dict.fromkeys([*(diagnosis.get("missing_data") or []), *quality.get("missing_data", [])])) or ["多周期技术诊断"],
        "fundamental_score": fundamental.get("fundamental_score"),
        "fundamental_risk_level": fundamental.get("risk_level", "unknown"),
        "fundamental_data_quality": fundamental.get("data_quality", "missing"),
        "fundamental_support_evidence": fundamental.get("support_evidence") or [],
        "fundamental_opposing_evidence": fundamental.get("opposing_evidence") or [],
        "event_risks": fundamental.get("event_risks") or [],
        "fundamental_hard_risks": fundamental.get("hard_risks") or [],
        "goodwill_net_assets_ratio_pct": fundamental.get("goodwill_net_assets_ratio_pct"),
        "performance_forecast": fundamental.get("performance_forecast"),
        "major_contracts": fundamental.get("major_contracts") or [],
        "fundamental_missing_data": fundamental.get("missing_data") or ["基本面与风险事件"],
        "concept_tags": board.get("concept_tags") or [],
        "concept_strength_score": board.get("concept_strength_score"),
        "concept_evidence": board.get("concept_evidence") or [],
        "global_affected": bool(global_factor.get("global_affected")),
        "global_sector_score": global_factor.get("global_sector_score"),
        "global_impact": global_factor.get("global_impact"),
        "global_confidence": global_factor.get("global_confidence"),
        "global_sectors": global_factor.get("global_sectors") or [],
        "global_drivers": global_factor.get("global_drivers") or [],
        "blockers": blockers, "reasons": reasons,
        "generated_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=15 if status == "recommend" else 30)).isoformat(timespec="seconds"),
    }


def build_snapshot(
    shadow: dict[str, Any], pool: dict[str, Any], state_rows: dict[str, dict[str, Any]],
    now: datetime | None = None, settings: dict[str, Any] | None = None,
    fundamentals: dict[str, dict[str, Any]] | None = None,
    board_strength: dict[str, Any] | None = None,
    global_market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    settings = settings or {}
    fundamentals = fundamentals or {}
    board_strength = board_strength or {}
    global_market = global_market or {}
    stock_concepts = {str(row.get("symbol")): row.get("concept_tags") or [] for row in board_strength.get("stock_concepts") or []}
    concept_rows = {str(row.get("name")): row for row in (board_strength.get("dimensions") or {}).get("concept", [])}
    sectors = {str(row.get("direction") or row.get("label")): row for row in (pool.get("sector_rankings") or pool.get("active_sectors") or [])}
    active = {str(row.get("direction") or row.get("label")) for row in (pool.get("active_sectors") or [])}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in shadow.get("candidates") or []:
        grouped.setdefault(str(row.get("direction") or "未知"), []).append(row)
    rows: list[dict[str, Any]] = []
    for direction, members in grouped.items():
        sector = sectors.get(direction, {"direction": direction, "score": 0})
        for member in members:
            if active and direction not in active:
                continue
            role = str(member.get("role") or _role(members, member))
            state = _state(member, state_rows)
            intraday = state_rows.get(str(member.get("symbol")), {})
            merged = {**member, **{key: intraday[key] for key in (
                "vwap", "volume_ratio_5m", "return_5m_pct", "vwap_deviation_pct", "bar_time", "entered_at"
            ) if intraday.get(key) is not None}}
            merged["fundamental_diagnosis"] = fundamentals.get(str(member.get("symbol")), {})
            tags = stock_concepts.get(str(member.get("symbol")), [])
            ranked_concepts = sorted((concept_rows[tag] for tag in tags if tag in concept_rows), key=lambda item: _num(item.get("score")), reverse=True)
            merged["board_strength"] = {
                "concept_tags": tags[:8],
                "concept_strength_score": ranked_concepts[0].get("score") if ranked_concepts else None,
                "concept_evidence": [f"概念{item.get('name')}强度{_num(item.get('score')):.1f}" for item in ranked_concepts[:2]],
            }
            merged["global_factor"] = factor_for_stock(
                str(member.get("symbol") or ""), str(member.get("name") or ""), [direction, *tags], global_market
            )
            merged["state_entered_at"] = intraday.get("entered_at")
            rows.append(build_recommendation(
                merged, sector, role, state, now,
                risk_reward_min=float(settings.get("risk_reward_min", 2.0)),
                market_level=str((pool.get("market_sentiment") or {}).get("level") or "neutral"),
                max_state_age_minutes=int(settings.get("max_state_age_minutes", 12)),
                max_target_up_pct=float(settings.get("max_target_up_pct", 15)),
            ))
    rows.sort(key=lambda item: (item["recommendation_status"] == "recommend", item["recommendation_score"]), reverse=True)
    return {
        "schema_version": 1, "generated_at": now.isoformat(timespec="seconds"),
        "mode": str(settings.get("mode", "shadow")), "engine_version": "v7.4",
        "market_sentiment": pool.get("market_sentiment") or {},
        "global_market": {"generated_at": global_market.get("generated_at"), "stale": global_market.get("stale", True),
                          "global_score": (global_market.get("market_summary") or {}).get("global_score")},
        "recommendations": rows,
        "summary": {
            "recommend": sum(x["recommendation_status"] == "recommend" for x in rows),
            "watch": sum(x["recommendation_status"] == "watch" for x in rows),
            "reject": sum(x["recommendation_status"] == "reject" for x in rows),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A股影子推荐引擎")
    parser.add_argument("--shadow", default="/root/.hermes/scripts/a_share_shadow_snapshot.json")
    parser.add_argument("--pool", default="/root/.hermes/scripts/a_share_dynamic_pool.json")
    parser.add_argument("--output", default="/root/.hermes/scripts/a_share_recommendation_snapshot.json")
    parser.add_argument("--states", default="/root/.hermes/scripts/a_share_market_snapshots.db")
    parser.add_argument("--config", default="/root/.hermes/scripts/a_share_alert_runtime_config.json")
    parser.add_argument("--fundamental", default="/root/.hermes/scripts/a_share_fundamental_snapshot.json")
    parser.add_argument("--board-strength", default="/root/.hermes/scripts/a_share_board_strength_snapshot.json")
    parser.add_argument("--global-market", default="/root/.hermes/scripts/a_share_global_market_factor.json")
    parser.add_argument("--ignore-session", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except Exception:
        config = {}
    settings = config.get("recommendation_engine", {}) if isinstance(config, dict) else {}
    fundamental_settings = config.get("fundamental_data", {}) if isinstance(config, dict) else {}
    if not args.ignore_session and not is_trading_session(now):
        return 0
    shadow = json.loads(Path(args.shadow).read_text(encoding="utf-8"))
    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    fundamental_payload: dict[str, Any] = {}
    fundamental_rows: dict[str, dict[str, Any]] = {}
    try:
        fundamental_payload = json.loads(Path(args.fundamental).read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(fundamental_payload.get("generated_at")))
        max_age_hours = float(fundamental_settings.get("max_age_hours", 36))
        if (now - generated).total_seconds() <= max_age_hours * 3600:
            fundamental_rows = {
                str(row.get("symbol")): row for row in fundamental_payload.get("stocks") or [] if row.get("symbol")
            }
    except Exception:
        fundamental_payload = {}
    board_payload: dict[str, Any] = {}
    try:
        board_payload = json.loads(Path(args.board_strength).read_text(encoding="utf-8"))
    except Exception:
        board_payload = {}
    global_payload = load_snapshot(Path(args.global_market), max_age_minutes=20, now=now)
    if global_payload.get("stale"):
        global_payload = {"generated_at": global_payload.get("generated_at"), "stale": True}
    if not args.ignore_session:
        max_input_age = int(settings.get("max_input_age_minutes", 12))
        if not is_fresh(shadow.get("generated_at"), now, max_input_age) or not is_fresh(pool.get("generated_at"), now, max_input_age):
            return 0
    state_rows: dict[str, dict[str, Any]] = {}
    store_error = None
    try:
        with SnapshotStore(args.states) as store:
            for row in store.connection.execute("SELECT symbol,state,bar_time,entered_at,metrics_json FROM intraday_states"):
                item = dict(row)
                try:
                    item.update(json.loads(item.pop("metrics_json")))
                except Exception:
                    item.pop("metrics_json", None)
                state_rows[str(item["symbol"])] = item

            payload = build_snapshot(shadow, pool, state_rows, now, settings, fundamental_rows, board_payload, global_payload)
            payload["fundamental"] = {
                "generated_at": fundamental_payload.get("generated_at"),
                "stale": not bool(fundamental_rows),
                "stocks": len(fundamental_rows),
            }
            payload["board_strength"] = {
                "generated_at": board_payload.get("generated_at"),
                "stale": not bool(board_payload.get("dimensions")),
                "concept_count": len((board_payload.get("dimensions") or {}).get("concept", [])),
            }
            store.save_technical_diagnoses(payload["generated_at"], shadow.get("candidates") or [])
            holdings: set[str] = set()
            try:
                portfolio_db = Path(config.get("portfolio_db", "/var/lib/a-share-dashboard/portfolios.db"))
                connection = sqlite3.connect(f"file:{portfolio_db}?mode=ro", uri=True, timeout=3)
                holdings = {str(row[0]) for row in connection.execute("SELECT DISTINCT symbol FROM positions WHERE quantity>0")}
                connection.close()
            except Exception:
                pass
            context = lifecycle_context(store.connection, now.date().isoformat(), holdings)
            market_level = str((payload.get("market_sentiment") or {}).get("level") or "neutral")
            payload["recommendations"] = apply_portfolio_guard(
                payload["recommendations"], settings.get("portfolio_guard", {}), context, market_level,
            )
            for row in payload["recommendations"]:
                row["market_level"] = market_level
            update_lifecycle(store.connection, payload["recommendations"], now, payload["mode"], payload["engine_version"])
            for row in payload["recommendations"]:
                if row.get("recommendation_status") == "recommend" and row.get("lifecycle_status") not in {"active", "entry_reached"}:
                    row["recommendation_status"] = "watch"
                    row["status_label"] = "观察"
                    row.setdefault("blockers", []).append(f"生命周期：{row.get('lifecycle_status') or '不可执行'}")
            payload["summary"] = {
                status: sum(row.get("recommendation_status") == status for row in payload["recommendations"])
                for status in ("recommend", "watch", "reject")
            }
            payload["portfolio"] = {
                "active_before": len(context.get("active_symbols") or []),
                "daily_new_before": context.get("daily_new_count", 0),
            }
            store.save_recommendations(payload["generated_at"], payload["recommendations"], payload["mode"])
            store.prune(int(settings.get("retention_days", 90)))
    except Exception as exc:
        store_error = str(exc)
        payload = build_snapshot(shadow, pool, state_rows, now, settings, fundamental_rows, board_payload, global_payload)
    _atomic_json(Path(args.output), payload)
    if store_error:
        payload["storage_error"] = store_error
        _atomic_json(Path(args.output), payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
