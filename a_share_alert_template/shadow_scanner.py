#!/usr/bin/env python3
"""A 股影子扫描器。

只生成可视化数据，不发送交易消息。它把固定池中的标的按
观察 -> 启动 -> 确认 -> 过热 分层，并使用盘中同期量比避免把上午的
累计成交量与完整日均量直接比较。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from data_source import fetch_history, fetch_snapshot
from strategies import compute_rsi
from technical_diagnosis import diagnose_technical


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_shadow_snapshot.json")


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


def expected_volume_fraction(now: datetime) -> float:
    """A 股典型 U 型成交分布的累计占比，用于同期量比。"""
    minute = now.hour * 60 + now.minute + now.second / 60
    anchors = [
        (570, 0.02),   # 09:30，给集合竞价留少量占比
        (600, 0.20),
        (630, 0.32),
        (690, 0.48),
        (780, 0.48),   # 午休
        (810, 0.62),
        (870, 0.82),
        (900, 1.00),
    ]
    if minute <= anchors[0][0]:
        return anchors[0][1]
    if minute >= anchors[-1][0]:
        return 1.0
    for (left_m, left_v), (right_m, right_v) in zip(anchors, anchors[1:]):
        if left_m <= minute <= right_m:
            if right_m == left_m or right_v == left_v:
                return left_v
            ratio = (minute - left_m) / (right_m - left_m)
            return left_v + (right_v - left_v) * ratio
    return 1.0


def _is_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 570 <= minute <= 690 or 780 <= minute <= 900


def _load_pool(config: dict[str, Any]) -> list[dict[str, Any]]:
    dynamic_cfg = config.get("dynamic_pool", {}) if isinstance(config, dict) else {}
    dynamic_path = Path(dynamic_cfg.get("file", "/root/.hermes/scripts/a_share_dynamic_pool.json"))
    if dynamic_cfg.get("enabled", False) and dynamic_cfg.get("replace_static_pool", True):
        try:
            payload = json.loads(dynamic_path.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(str(payload.get("generated_at")))
            max_age = int(dynamic_cfg.get("max_age_minutes", 360))
            active = payload.get("active_sectors") or []
            if (datetime.now() - generated).total_seconds() <= max_age * 60 and active:
                return [
                    {
                        "direction": str(item.get("direction") or item.get("label") or "未知板块"),
                        "members": [
                            {
                                "symbol": str(member.get("symbol")),
                                "name": member.get("name") or member.get("symbol"),
                                "type": member.get("type", "stock"),
                                "role": member.get("role"),
                            }
                            for member in item.get("members", [])
                            if member.get("symbol")
                        ],
                    }
                    for item in active
                    if item.get("members")
                ]
        except Exception:
            pass
    merged: dict[str, dict[str, Any]] = {}
    for source in ("direction_pool", "message_focus_pool"):
        for item in config.get(source, []) or []:
            direction = str(item.get("direction") or "未知")
            bucket = merged.setdefault(direction, {"direction": direction, "members": []})
            seen = {m["symbol"] for m in bucket["members"]}
            for member in item.get("members", []) or []:
                symbol = str(member.get("symbol") or "")
                if symbol and symbol not in seen:
                    bucket["members"].append({
                        "symbol": symbol,
                        "name": member.get("name") or symbol,
                        "type": member.get("type", "stock"),
                        "role": member.get("role"),
                    })
                    seen.add(symbol)
    return list(merged.values())


def analyze_member(
    member: dict[str, Any],
    direction: str,
    snapshot: dict[str, Any],
    history: pd.DataFrame,
    now: datetime,
) -> dict[str, Any] | None:
    if history is None or history.empty or len(history) < 20:
        return None
    closes = pd.to_numeric(history["close"], errors="coerce").dropna()
    volumes = pd.to_numeric(history["volume"], errors="coerce").dropna()
    highs = pd.to_numeric(history["high"], errors="coerce").dropna()
    lows = pd.to_numeric(history["low"], errors="coerce").dropna()
    if len(closes) < 20 or len(volumes) < 5:
        return None

    price = _num(snapshot.get("price"))
    open_price = _num(snapshot.get("open"))
    high = _num(snapshot.get("high"))
    low = _num(snapshot.get("low"))
    amount = _num(snapshot.get("amount"))
    change = _num(snapshot.get("change_pct"))
    if min(price, high, low) <= 0:
        return None

    ma5 = _num(closes.tail(5).mean())
    ma20 = _num(closes.tail(20).mean())
    avg_volume = _num(volumes.tail(5).mean())
    current_volume = amount / price if price > 0 else 0.0
    fraction = expected_volume_fraction(now)
    same_time_volume_ratio = current_volume / (avg_volume * fraction) if avg_volume > 0 and fraction > 0 else 0.0
    ma5_bias = (price / ma5 - 1) * 100 if ma5 else 0.0
    ma20_bias = (price / ma20 - 1) * 100 if ma20 else 0.0
    previous_high = _num(highs.iloc[-1])
    high20 = _num(highs.tail(20).max())
    low20 = _num(lows.tail(20).min())
    range20 = (high20 / low20 - 1) * 100 if low20 else 99.0
    breakout_prev_high = (price / previous_high - 1) * 100 if previous_high else 0.0
    day_range = high - low
    close_position = (price - low) / day_range if day_range > 0 else 0.5
    body_ratio = abs(price - open_price) / day_range if day_range > 0 else 0.0
    rsi = _num(compute_rsi(pd.concat([closes, pd.Series([price])], ignore_index=True), 14).iloc[-1])
    diagnosis = diagnose_technical(history, snapshot)

    setup = (
        range20 <= 16
        and abs(ma20_bias) <= 5
        and price >= ma5 * 0.97
    )
    starting = (
        same_time_volume_ratio >= 1.15
        and -0.5 <= change <= 3.2
        and breakout_prev_high >= -0.6
        and close_position >= 0.55
    )
    confirmed = (
        same_time_volume_ratio >= 1.35
        and change >= 0.8
        and breakout_prev_high >= 0
        and close_position >= 0.68
        and body_ratio >= 0.18
    )
    overheated = change >= 5.0 or rsi >= 76 or ma5_bias >= 5.5

    if overheated:
        stage = "overheated"
        stage_label = "过热"
    elif confirmed:
        stage = "confirmed"
        stage_label = "确认"
    elif starting:
        stage = "starting"
        stage_label = "启动"
    elif setup:
        stage = "watch"
        stage_label = "观察"
    else:
        stage = "dormant"
        stage_label = "休眠"

    # 每个维度独立封顶，避免对同一次上涨重复无限加分。
    structure_score = max(0.0, 25 - abs(ma20_bias) * 2.5) if setup else max(0.0, 12 - abs(ma20_bias))
    volume_score = min(max((same_time_volume_ratio - 0.8) * 25, 0), 25)
    trigger_score = min(max((breakout_prev_high + 1.0) * 8, 0), 20)
    quality_score = min(max(close_position * 15, 0), 15)
    trend_score = min(max((change + 0.5) * 5, 0), 15)
    score = structure_score + volume_score + trigger_score + quality_score + trend_score
    if overheated:
        score -= min(max(change - 3.5, 0) * 5 + max(ma5_bias - 4, 0) * 3, 20)

    reasons: list[str] = []
    if setup:
        reasons.append(f"20日波动收敛至{range20:.1f}%")
    if same_time_volume_ratio >= 1.15:
        reasons.append(f"同期量比{same_time_volume_ratio:.2f}")
    if breakout_prev_high >= 0:
        reasons.append(f"突破昨高{breakout_prev_high:.2f}%")
    if close_position >= 0.68:
        reasons.append(f"日内位置{close_position:.2f}")
    if overheated:
        reasons.append("追涨风险偏高")

    return {
        "direction": direction,
        "symbol": member["symbol"],
        "name": member["name"],
        "type": member.get("type", "stock"),
        "role": member.get("role"),
        "stage": stage,
        "stage_label": stage_label,
        "score": round(max(score, 0), 2),
        "price": round(price, 3),
        "low": round(low, 3),
        "high": round(high, 3),
        "volume_available": bool(amount > 0 and avg_volume > 0),
        "previous_high": round(previous_high, 3),
        "high20": round(high20, 3),
        "change_pct": round(change, 2),
        "same_time_volume_ratio": round(same_time_volume_ratio, 2),
        "expected_volume_fraction": round(fraction, 3),
        "ma5_bias_pct": round(ma5_bias, 2),
        "ma20_bias_pct": round(ma20_bias, 2),
        "rsi_live": round(rsi, 2),
        "breakout_prev_high_pct": round(breakout_prev_high, 2),
        "close_position": round(close_position, 2),
        "range20_pct": round(range20, 2),
        "technical_score": diagnosis.get("technical_score"),
        "technical_trend": diagnosis.get("trend"),
        "support_price": diagnosis.get("support_price"),
        "resistance_price": diagnosis.get("resistance_price"),
        "technical_diagnosis": diagnosis,
        "reasons": reasons,
    }


def scan(config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    pool = _load_pool(config)
    members_by_symbol: dict[str, dict[str, Any]] = {}
    directions_by_symbol: dict[str, list[str]] = {}
    for item in pool:
        for member in item["members"]:
            members_by_symbol.setdefault(member["symbol"], member)
            directions_by_symbol.setdefault(member["symbol"], []).append(item["direction"])
    symbols = sorted(members_by_symbol)
    snapshots = fetch_snapshot(symbols) if symbols else {}
    histories: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = {executor.submit(fetch_history, symbol, 65): symbol for symbol in symbols}
        for future in as_completed(tasks):
            symbol = tasks[future]
            try:
                histories[symbol] = future.result()
            except Exception:
                continue

    candidates: list[dict[str, Any]] = []
    for symbol, member in members_by_symbol.items():
        snapshot = snapshots.get(symbol)
        history = histories.get(symbol)
        if not snapshot or history is None:
            continue
        # 同一股票可属于多个方向，指标只算一次，再映射到各方向。
        for direction in directions_by_symbol[symbol]:
            item = analyze_member(member, direction, snapshot, history, now)
            if item:
                candidates.append(item)

    stage_rank = {"starting": 4, "confirmed": 3, "watch": 2, "overheated": 1, "dormant": 0}
    candidates.sort(key=lambda item: (stage_rank.get(item["stage"], 0), item["score"]), reverse=True)
    direction_rows: list[dict[str, Any]] = []
    for direction in sorted({item["direction"] for item in candidates}):
        rows = [item for item in candidates if item["direction"] == direction]
        changes = [item["change_pct"] for item in rows]
        breadth = sum(1 for value in changes if value > 0) / len(changes) if changes else 0
        active = sum(1 for item in rows if item["stage"] in {"starting", "confirmed"})
        overheated = sum(1 for item in rows if item["stage"] == "overheated")
        best = max(rows, key=lambda item: item["score"]) if rows else None
        direction_score = (
            breadth * 35
            + min(max((sum(changes) / len(changes) if changes else 0) + 0.5, 0) * 10, 25)
            + min(active * 12, 24)
            + (min(best["same_time_volume_ratio"], 2) * 8 if best else 0)
            - overheated * 5
        )
        direction_rows.append({
            "direction": direction,
            "score": round(max(direction_score, 0), 2),
            "breadth_pct": round(breadth * 100, 1),
            "median_change_pct": round(float(pd.Series(changes).median()), 2) if changes else 0,
            "active_count": active,
            "overheated_count": overheated,
            "member_count": len(rows),
            "best_symbol": best["symbol"] if best else None,
            "best_name": best["name"] if best else None,
        })
    direction_rows.sort(key=lambda item: item["score"], reverse=True)

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "market_session": _is_session(now),
        "source_scope": "configured_pool",
        "symbols_expected": len(symbols),
        "symbols_available": len(histories),
        "directions": direction_rows,
        "candidates": candidates,
        "stage_counts": {
            stage: sum(1 for item in candidates if item["stage"] == stage)
            for stage in ("watch", "starting", "confirmed", "overheated", "dormant")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A股影子扫描")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ignore-session", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    output = Path(args.output)
    if not args.ignore_session and not _is_session(now):
        # 盘外保留最后一份有效快照，不用空数据覆盖。
        return 0
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    _atomic_json(output, scan(config, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
