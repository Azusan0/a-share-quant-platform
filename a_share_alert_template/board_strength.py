#!/usr/bin/env python3
"""行业/概念双维度强度快照（东财 push2，影子用途）。"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from data_source import fetch_snapshot
from snapshot_store import SnapshotStore


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_board_strength_snapshot.json")
DEFAULT_POOL = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
DEFAULT_SHADOW = Path("/root/.hermes/scripts/a_share_shadow_snapshot.json")
DEFAULT_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")
PUSH2_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
PUSH2_SLIST = "https://push2.eastmoney.com/api/qt/slist/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


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


def _members(pool: dict[str, Any]) -> list[dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for sector in pool.get("active_sectors") or []:
        direction = str(sector.get("direction") or sector.get("label") or "未知")
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol") or "")
            if symbol:
                result[symbol] = {"symbol": symbol, "name": str(member.get("name") or symbol), "industry": direction}
    return list(result.values())


def _secid(symbol: str) -> str:
    return f"1.{symbol}" if symbol.startswith(("5", "6", "9")) else f"0.{symbol}"


def fetch_stock_blocks(symbol: str, timeout: int = 15) -> list[dict[str, Any]]:
    params = {
        "fltt": "2", "invt": "2", "secid": _secid(symbol), "spt": "3", "pi": "0", "pz": "200", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    response = requests.get(PUSH2_SLIST, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    data = (response.json().get("data") or {}).get("diff") or {}
    items = data.values() if isinstance(data, dict) else data
    return [{"name": str(item.get("f14") or ""), "code": str(item.get("f12") or ""),
             "change_pct": _num(item.get("f3")), "leader": str(item.get("f128") or "")} for item in items]


def fetch_board_flow(period: str = "today", top_n: int = 200, timeout: int = 15) -> list[dict[str, Any]]:
    period_fields = {
        "today": ("f62", "f184", "f3", "f204"),
        "5d": ("f164", "f165", "f109", "f257"),
    }
    main_field, pct_field, change_field, leader_field = period_fields[period]
    params = {
        "pn": "1", "pz": str(min(max(top_n, 20), 200)), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": main_field, "fs": "m:90+t:3", "fields": f"f12,f14,{change_field},{main_field},{pct_field},{leader_field}",
    }
    response = requests.get(PUSH2_CLIST, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    data = response.json().get("data") or {}
    items = data.get("diff") or []
    if isinstance(items, dict):
        items = list(items.values())
    return [{"name": str(item.get("f14") or ""), "code": str(item.get("f12") or ""),
             "change_pct": _num(item.get(change_field)), "main_net": _num(item.get(main_field)),
             "main_pct": _num(item.get(pct_field)), "leader": str(item.get(leader_field) or ""),
             "period": period} for item in items[:top_n]]


def classify_concepts(blocks: list[dict[str, Any]], industry: str) -> list[dict[str, Any]]:
    """东财返回行业/概念/地域混合列表，剔除当前行业和明显地域标签。"""
    region_words = ("板块", "地区", "新区", "经济圈", "自贸区", "特区")
    style_prefixes = ("MSCI", "富时", "标准普尔", "中证", "上证", "深证", "深成", "沪深", "HS")
    style_names = {"融资融券", "大盘股", "中盘股", "小盘股", "低价股", "高价股", "破净股", "高股息", "基金重仓", "机构重仓", "社保重仓", "QFII重仓", "昨日高振幅", "科技风格", "成长风格", "价值风格"}
    result = []
    for block in blocks:
        name = str(block.get("name") or "").strip()
        if not name or name == industry or name in {"沪股通", "深股通"} or name in style_names:
            continue
        if name.startswith(style_prefixes) or name.startswith("昨日"):
            continue
        if any(word in name for word in region_words):
            continue
        result.append(block)
    return result


def score_board(
    name: str, code: str, members: list[dict[str, Any]], flow_today: dict[str, Any] | None,
    flow_5d: dict[str, Any] | None,
) -> dict[str, Any]:
    changes = [_num(row.get("change_pct")) for row in members]
    breadth = sum(value > 0 for value in changes) / len(changes) * 100 if changes else 0
    median_change = float(pd.Series(changes).median()) if changes else 0
    today_net = _num((flow_today or {}).get("main_net"))
    five_day_net = _num((flow_5d or {}).get("main_net"))
    today_pct = _num((flow_today or {}).get("main_pct"))
    persistence = (1 if today_net > 0 else 0) + (1 if five_day_net > 0 else 0)
    score = min(max(breadth, 0) * 0.35, 35) + min(max(median_change + 1, 0) * 5, 20)
    score += 10 if today_net > 0 else 0
    score += 10 if five_day_net > 0 else 0
    score += min(max(today_pct, 0), 5) * 2
    score += min(len(members) / 3, 1) * 10
    return {
        "name": name, "code": code, "score": round(min(max(score, 0), 100), 2),
        "breadth_pct": round(breadth, 1), "median_change_pct": round(median_change, 2),
        "main_net_today": round(today_net, 2), "main_pct_today": round(today_pct, 2),
        "main_net_5d": round(five_day_net, 2), "persistence": persistence,
        "member_count": len(members), "members": members,
    }


def collect(pool: dict[str, Any], shadow: dict[str, Any], now: datetime, request_interval: float = 0.8) -> dict[str, Any]:
    members = _members(pool)
    symbols = [row["symbol"] for row in members]
    snapshots = fetch_snapshot(symbols) if symbols else {}
    change_by_symbol = {str(row.get("symbol")): _num(row.get("change_pct")) for row in shadow.get("candidates") or []}
    blocks_by_symbol: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for member in members:
        try:
            blocks_by_symbol[member["symbol"]] = classify_concepts(fetch_stock_blocks(member["symbol"]), member["industry"])
        except Exception as exc:
            errors[member["symbol"]] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if request_interval > 0:
            time.sleep(request_interval)
    flow_errors: dict[str, str] = {}
    try:
        flow_today = {row["name"]: row for row in fetch_board_flow("today")}
    except Exception as exc:
        flow_today = {}
        flow_errors["today"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        flow_5d = {row["name"]: row for row in fetch_board_flow("5d")}
    except Exception as exc:
        flow_5d = {}
        flow_errors["5d"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    concept_members: dict[str, list[dict[str, Any]]] = {}
    stock_tags: dict[str, list[str]] = {}
    for member in members:
        tags = []
        for block in blocks_by_symbol.get(member["symbol"], []):
            name = block["name"]
            tags.append(name)
            concept_members.setdefault(name, []).append({
                "symbol": member["symbol"], "name": member["name"],
                "change_pct": change_by_symbol.get(member["symbol"], _num(snapshots.get(member["symbol"], {}).get("change_pct"))),
            })
        stock_tags[member["symbol"]] = tags
    rows = []
    for name, board_members in concept_members.items():
        flow_name = flow_today.get(name) or flow_5d.get(name) or {}
        rows.append(score_board(name, str(flow_name.get("code") or ""), board_members, flow_today.get(name), flow_5d.get(name)))
    rows.sort(key=lambda row: (row["score"], row["member_count"]), reverse=True)
    industry_rows = []
    for sector in pool.get("active_sectors") or []:
        name = str(sector.get("direction") or sector.get("label") or "")
        flow_name = flow_today.get(name) or flow_5d.get(name) or {}
        industry_rows.append({
            "name": name, "code": str(flow_name.get("code") or ""), "score": _num(sector.get("score")),
            "breadth_pct": _num(sector.get("breadth_pct")), "median_change_pct": _num(sector.get("median_change_pct")),
            "main_net_today": _num(flow_today.get(name, {}).get("main_net")), "main_net_5d": _num(flow_5d.get(name, {}).get("main_net")),
            "member_count": len(sector.get("members") or []), "persistence": int(flow_today.get(name, {}).get("main_net", 0) > 0) + int(flow_5d.get(name, {}).get("main_net", 0) > 0),
        })
    return {
        "schema_version": 1, "generated_at": now.isoformat(timespec="seconds"), "source_scope": "active_dynamic_pool",
        "dimensions": {"industry": sorted(industry_rows, key=lambda row: row["score"], reverse=True), "concept": rows[:100]},
        "stock_concepts": [{"symbol": symbol, "concept_tags": tags} for symbol, tags in stock_tags.items()],
        "summary": {"industry_count": len(industry_rows), "concept_count": len(rows), "stocks": len(symbols), "concept_errors": len(errors)},
        "source_health": {"errors": errors, "flow_errors": flow_errors, "concept_flow_today": len(flow_today), "concept_flow_5d": len(flow_5d)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="行业/概念双维度强度快照")
    parser.add_argument("--pool", default=str(DEFAULT_POOL)); parser.add_argument("--shadow", default=str(DEFAULT_SHADOW))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT)); parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--max-age-hours", type=float, default=6); parser.add_argument("--force", action="store_true")
    args = parser.parse_args(); now = datetime.now(); output = Path(args.output)
    pool = _load(Path(args.pool)); shadow = _load(Path(args.shadow))
    symbols = {row["symbol"] for row in _members(pool)}; existing = _load(output)
    if not args.force:
        try:
            generated = datetime.fromisoformat(str(existing.get("generated_at")))
            cached = {row.get("symbol") for row in existing.get("stock_concepts") or []}
            if generated.date() == now.date() and (now - generated).total_seconds() < args.max_age_hours * 3600 and symbols.issubset(cached):
                return 0
        except Exception:
            pass
    payload = collect(pool, shadow, now)
    with SnapshotStore(args.db) as store:
        store.save_board_strength(payload["generated_at"], payload)
        store.prune(90)
    _atomic_json(output, payload); return 0


if __name__ == "__main__":
    raise SystemExit(main())
