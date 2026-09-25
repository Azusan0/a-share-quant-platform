#!/usr/bin/env python3
"""根据行业强度自动轮换 Hermes 实际监控池。"""
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

import akshare as ak
import pandas as pd


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
SENTIMENT_PATH = Path("/root/.hermes/scripts/a_share_market_sentiment.json")


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


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _is_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 570 <= minute <= 690 or 780 <= minute <= 900


def _percentile(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").fillna(0)
    return numeric.rank(pct=True, method="average")


def _normalize_spot(frame: pd.DataFrame, previous: dict[str, Any]) -> list[dict[str, Any]]:
    old = {row.get("label"): row for row in previous.get("sector_rankings", [])}
    data = frame.copy()
    data["change"] = pd.to_numeric(data["涨跌幅"], errors="coerce").fillna(0)
    data["amount"] = pd.to_numeric(data["总成交额"], errors="coerce").fillna(0)
    data["companies"] = pd.to_numeric(data["公司家数"], errors="coerce").fillna(1).clip(lower=1)
    data["amount_per_company"] = data["amount"] / data["companies"]
    data["liquidity_pct"] = _percentile(data["amount_per_company"])
    rows: list[dict[str, Any]] = []
    for _, raw in data.iterrows():
        label = str(raw.get("label") or "")
        name = str(raw.get("板块") or label)
        change = _num(raw.get("change"))
        leader_change = _num(raw.get("个股-涨跌幅"))
        previous_change = _num((old.get(label) or {}).get("change_pct"), change)
        acceleration = change - previous_change
        base_score = (
            min(max((change + 1.0) * 9, 0), 36)
            + min(max(acceleration * 10 + 6, 0), 15)
            + _num(raw.get("liquidity_pct")) * 12
            + min(max(leader_change, 0) * 1.5, 10)
        )
        if "ST" in str(raw.get("股票名称") or "").upper():
            base_score -= 6
        if change >= 6 or leader_change >= 15:
            base_score -= 8
        rows.append({
            "label": label,
            "direction": name,
            "base_score": round(max(base_score, 0), 2),
            "change_pct": round(change, 3),
            "acceleration_pct": round(acceleration, 3),
            "company_count": int(_num(raw.get("公司家数"))),
            "amount": round(_num(raw.get("总成交额")), 2),
            "leader_symbol": str(raw.get("股票代码") or "").replace("sh", "").replace("sz", ""),
            "leader_name": str(raw.get("股票名称") or ""),
            "leader_change_pct": round(leader_change, 3),
        })
    rows.sort(key=lambda row: row["base_score"], reverse=True)
    return rows


def _limit_pct(symbol: str) -> float:
    return 19.5 if symbol.startswith(("30", "68")) else 9.5


def select_members(frame: pd.DataFrame, sector: dict[str, Any], count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if frame is None or frame.empty:
        return [], {"breadth_pct": 0, "median_change_pct": 0}
    rows: list[dict[str, Any]] = []
    changes: list[float] = []
    for _, raw in frame.iterrows():
        symbol = str(raw.get("code") or "")
        name = str(raw.get("name") or symbol)
        change = _num(raw.get("changepercent"))
        changes.append(change)
        price = _num(raw.get("trade"))
        amount = _num(raw.get("amount"))
        turnover = _num(raw.get("turnoverratio"))
        high = _num(raw.get("high"))
        low = _num(raw.get("low"))
        close_position = (price - low) / (high - low) if high > low and price > 0 else 0.5
        if not symbol.startswith(("00", "30", "60", "68")):
            continue
        if "ST" in name.upper() or price < 2 or amount < 80_000_000:
            continue
        if change >= _limit_pct(symbol) and close_position >= 0.98:
            continue
        relative = change - _num(sector.get("change_pct"))
        turnover_quality = min(max(turnover, 0), 12) / 12 * 18
        amount_score = min(max(math.log10(max(amount, 1)) - 7.5, 0) * 12, 24)
        score = (
            min(max(relative + 2, 0) * 6, 24)
            + turnover_quality
            + amount_score
            + min(max(close_position, 0), 1) * 18
        )
        if change > 8 or turnover > 25:
            score -= 12
        rows.append({
            "symbol": symbol,
            "name": name,
            "type": "stock",
            "selection_score": round(max(score, 0), 2),
            "change_pct": round(change, 2),
            "relative_strength_pct": round(relative, 2),
            "amount": round(amount, 2),
            "turnover_pct": round(turnover, 2),
            "close_position": round(close_position, 2),
        })
    rows.sort(key=lambda row: row["selection_score"], reverse=True)
    selected: list[dict[str, Any]] = []
    if rows:
        leader = dict(rows[0])
        leader["role"] = "leader"
        selected.append(leader)
    remaining = [row for row in rows if row["symbol"] not in {item["symbol"] for item in selected}]
    if remaining and len(selected) < count:
        core = max(remaining, key=lambda row: row["selection_score"] + min(row["amount"] / 100_000_000, 8) - max(row["turnover_pct"] - 15, 0))
        core = dict(core)
        core["role"] = "core"
        selected.append(core)
    remaining = [row for row in rows if row["symbol"] not in {item["symbol"] for item in selected}]
    if remaining and len(selected) < count:
        median_change = float(pd.Series([row["change_pct"] for row in rows]).median())
        catchup_rows = [row for row in remaining if row["change_pct"] <= median_change and row["close_position"] >= .45]
        catchup = dict(max(catchup_rows or remaining, key=lambda row: row["selection_score"] - max(row["change_pct"] - 4, 0) * 3))
        catchup["role"] = "catch_up"
        selected.append(catchup)
    for row in rows:
        if len(selected) >= count:
            break
        if row["symbol"] not in {item["symbol"] for item in selected}:
            item = dict(row)
            item["role"] = "core"
            selected.append(item)
    breadth = sum(1 for value in changes if value > 0) / len(changes) * 100 if changes else 0
    median = float(pd.Series(changes).median()) if changes else 0
    return selected[:count], {
        "breadth_pct": round(breadth, 1),
        "median_change_pct": round(median, 2),
        "eligible_count": len(rows),
    }


def _sector_score(sector: dict[str, Any], detail: dict[str, Any]) -> float:
    score = _num(sector.get("base_score"))
    score += _num(detail.get("breadth_pct")) / 100 * 25
    score += min(max(_num(detail.get("median_change_pct")) + 0.5, 0) * 5, 12)
    return round(min(max(score, 0), 100), 2)


def rotate(
    ranked: list[dict[str, Any]],
    previous: dict[str, Any],
    now: datetime,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    max_sectors = int(params.get("max_sectors", 5))
    enter_score = float(params.get("enter_score", 45))
    exit_score = float(params.get("exit_score", 32))
    hard_exit_score = float(params.get("hard_exit_score", 18))
    replacement_margin = float(params.get("replacement_margin", 8))
    min_tenure_minutes = int(params.get("min_tenure_minutes", 60))
    current_by_label = {row["label"]: dict(row) for row in previous.get("active_sectors", [])}
    ranked_by_label = {row["label"]: row for row in ranked}
    active: list[dict[str, Any]] = []
    rotations: list[dict[str, Any]] = list(previous.get("rotation_history", []))[-99:]

    for label, old in current_by_label.items():
        latest = ranked_by_label.get(label)
        if not latest or not latest.get("members"):
            preserved = dict(old)
            preserved["data_stale"] = True
            active.append(preserved)
            continue
        entered_at = old.get("entered_at") or previous.get("generated_at") or now.isoformat(timespec="seconds")
        try:
            tenure = (now - datetime.fromisoformat(entered_at)).total_seconds() / 60
        except Exception:
            tenure = min_tenure_minutes
        latest = dict(latest)
        latest["entered_at"] = entered_at
        latest["tenure_minutes"] = round(max(tenure, 0), 1)
        if latest["score"] < hard_exit_score or (tenure >= min_tenure_minutes and latest["score"] < exit_score):
            rotations.append({"at": now.isoformat(timespec="seconds"), "action": "exit", "direction": latest["direction"], "score": latest["score"], "reason": "强度跌破退出线"})
            continue
        active.append(latest)

    eligible = [row for row in ranked if row.get("members") and row["score"] >= enter_score]
    active_labels = {row["label"] for row in active}
    challengers = [row for row in eligible if row["label"] not in active_labels]
    for challenger in challengers:
        if len(active) < max_sectors:
            item = dict(challenger)
            item["entered_at"] = now.isoformat(timespec="seconds")
            item["tenure_minutes"] = 0
            active.append(item)
            active_labels.add(item["label"])
            rotations.append({"at": now.isoformat(timespec="seconds"), "action": "enter", "direction": item["direction"], "score": item["score"], "reason": "进入强度前列"})
            continue
        replaceable = [row for row in active if _num(row.get("tenure_minutes")) >= min_tenure_minutes]
        if not replaceable:
            break
        weakest = min(replaceable, key=lambda row: row["score"])
        if challenger["score"] < weakest["score"] + replacement_margin:
            continue
        active.remove(weakest)
        item = dict(challenger)
        item["entered_at"] = now.isoformat(timespec="seconds")
        item["tenure_minutes"] = 0
        active.append(item)
        rotations.append({"at": now.isoformat(timespec="seconds"), "action": "rotate", "direction": item["direction"], "score": item["score"], "replaced": weakest["direction"], "reason": f"强度高出{replacement_margin:.0f}分"})

    active.sort(key=lambda row: row["score"], reverse=True)
    return active[:max_sectors], rotations[-100:]


def build_dynamic_pool(config: dict[str, Any], previous: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    params = config.get("dynamic_pool", {}) if isinstance(config, dict) else {}
    sentiment = _load(SENTIMENT_PATH)
    spot = ak.stock_sector_spot()
    base_ranked = _normalize_spot(spot, previous)
    candidate_count = int(params.get("candidate_sector_count", 12))
    stocks_per_sector = int(params.get("stocks_per_sector", 3))
    details: dict[str, pd.DataFrame] = {}
    previous_labels = {row.get("label") for row in previous.get("active_sectors", [])}
    detail_rows = [row for row in base_ranked if row["label"] in previous_labels]
    detail_labels = {row["label"] for row in detail_rows}
    for row in base_ranked[:candidate_count]:
        if row["label"] not in detail_labels:
            detail_rows.append(row)
            detail_labels.add(row["label"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = {
            executor.submit(ak.stock_sector_detail, sector=row["label"]): row["label"]
            for row in detail_rows
        }
        for future in as_completed(tasks):
            label = tasks[future]
            try:
                details[label] = future.result()
            except Exception:
                continue
    ranked: list[dict[str, Any]] = []
    for sector in base_ranked:
        members, metrics = select_members(details.get(sector["label"]), sector, stocks_per_sector)
        row = {**sector, **metrics, "members": members}
        row["score"] = _sector_score(sector, metrics)
        ranked.append(row)
    ranked.sort(key=lambda row: row["score"], reverse=True)
    sentiment_level = sentiment.get("level")
    sentiment_adjustments = {
        "risk_on": 0,
        "positive": 0,
        "neutral": 3,
        "weak": 8,
        "risk_off": 15,
    }
    adjustment = sentiment_adjustments.get(sentiment_level, 0)
    if adjustment:
        params = dict(params)
        params["enter_score"] = float(params.get("enter_score", 45)) + adjustment
        params["replacement_margin"] = float(params.get("replacement_margin", 8)) + adjustment / 2
    active, rotations = rotate(ranked, previous, now, params)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "provider": "sina_sector",
        "market_sentiment": {
            "level": sentiment_level,
            "label": sentiment.get("label"),
            "score": sentiment.get("score"),
            "enter_score_adjustment": adjustment,
        },
        "active_sectors": active,
        "sector_rankings": ranked,
        "rotation_history": rotations,
        "summary": {
            "sector_count": len(ranked),
            "active_count": len(active),
            "member_count": sum(len(row.get("members", [])) for row in active),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="按板块强度生成动态监控池")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--ignore-session", action="store_true")
    args = parser.parse_args()
    if not args.ignore_session and not _is_session(datetime.now()):
        return 0
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    output = Path(args.output)
    previous = _load(output)
    payload = build_dynamic_pool(config, previous)
    if not payload.get("active_sectors"):
        raise RuntimeError("动态池没有可用板块，保留上一份文件")
    _atomic_json(output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
