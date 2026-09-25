#!/usr/bin/env python3
"""动态池5分钟行情、状态机和SQLite落库的影子管线。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_data import fetch_m5_with_fallback
from intraday_state import evaluate_state
from snapshot_store import DEFAULT_DB, SnapshotStore


DEFAULT_POOL = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
MARKET_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")


def _direction_matches(direction: str, labels: list[str]) -> bool:
    """动态池方向只作扫描来源，必须与个股权威板块标签一致。"""
    if not labels:
        return True
    d = str(direction or "").replace("行业", "").replace("板块", "").replace("概念", "").strip().lower()
    aliases = {
        "煤炭": ("煤炭", "动力煤", "煤化工"), "石油": ("石油", "油气", "天然气", "页岩气", "油服"),
        "化纤": ("化纤", "化学纤维", "玻纤", "玻璃纤维"), "玻璃": ("玻璃", "玻纤"),
        "有色金属": ("有色", "铝", "钨", "小金属", "稀土", "贵金属", "黄金", "白银", "工业金属"),
    }.get(d, (d,))
    texts = [str(label).lower() for label in labels if label]
    if any(alias in text for alias in aliases for text in texts):
        return True
    # 标签未覆盖行业时保留候选，但对已知互斥行业硬拦截，修复医药被煤炭污染的问题。
    if d == "煤炭" and any(word in text for text in texts for word in ("医药", "医疗", "生物", "cro")):
        return False
    return True


def _validated_pool(pool: dict[str, Any], db_path: Path) -> tuple[dict[str, Any], set[str]]:
    """剔除 symbol/板块归属冲突成员，并返回本轮可信 symbol 集合。"""
    labels_by_symbol: dict[str, list[str]] = {}
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            symbols = {str(m.get("symbol")) for s in pool.get("active_sectors") or [] for m in s.get("members") or [] if m.get("symbol")}
            for symbol in symbols:
                row = conn.execute("SELECT concept_tags_json FROM stock_board_memberships WHERE symbol=? ORDER BY generated_at DESC LIMIT 1", (symbol,)).fetchone()
                if not row:
                    continue
                try:
                    labels_by_symbol[symbol] = [str(x) for x in json.loads(row[0] or "[]") if x]
                except (TypeError, json.JSONDecodeError):
                    pass
            conn.close()
        except Exception:
            labels_by_symbol = {}
    valid: set[str] = set()
    sectors: list[dict[str, Any]] = []
    for sector in pool.get("active_sectors") or []:
        direction = str(sector.get("direction") or sector.get("label") or "未知板块")
        members = []
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol") or "")
            labels = labels_by_symbol.get(symbol, [])
            if symbol and _direction_matches(direction, labels):
                members.append(member)
                valid.add(symbol)
        if members:
            sectors.append({**sector, "members": members})
    return {**pool, "active_sectors": sectors}, valid


def _members(pool: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sector in pool.get("active_sectors") or []:
        direction = str(sector.get("direction") or sector.get("label") or "未知板块")
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol") or "")
            if symbol:
                item = result.setdefault(symbol, {"symbol": symbol, "name": member.get("name") or symbol, "sectors": []})
                if direction not in item["sectors"]:
                    item["sectors"].append(direction)
    return result


def run(pool_path: Path, db_path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pool, valid_symbols = _validated_pool(pool, db_path)
    members = _members(pool)
    captured_at = now.isoformat(timespec="seconds")
    summary: dict[str, Any] = {"captured_at": captured_at, "symbols_expected": len(members), "symbols_ok": 0, "symbols_failed": 0, "transitions": 0, "errors": []}
    with SnapshotStore(db_path) as store:
        store.save_sector_snapshots(captured_at, pool.get("sector_rankings") or pool.get("active_sectors") or [], str(pool.get("provider") or "dynamic_pool"))
        results = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            tasks = {executor.submit(fetch_m5_with_fallback, symbol, 80, 6, 2, now): symbol for symbol in members}
            for future in as_completed(tasks):
                symbol = tasks[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:
                    summary["symbols_failed"] += 1
                    summary["errors"].append({"symbol": symbol, "error": str(exc)[:180]})
                    store.save_health("tencent_m5", captured_at, symbol, False, error=str(exc)[:500])
        for symbol, result in results.items():
            store.upsert_bars(result.bars)
            for attempt in result.attempts:
                store.save_health(attempt["provider"], result.fetched_at, symbol, attempt["ok"], attempt.get("latency_ms"),
                                  attempt.get("stale", False), attempt.get("fallback_level", 0), attempt.get("error"))
            store.save_health(result.provider, result.fetched_at, symbol, True, result.latency_ms, result.stale, result.fallback_level)
            bars = store.get_bars(symbol, result.bars[-1]["time"][:10], 80)
            try:
                previous = store.get_state(symbol)
                state = evaluate_state(bars, previous)
            except ValueError as exc:
                summary["symbols_failed"] += 1
                summary["errors"].append({"symbol": symbol, "error": str(exc)})
                continue
            item = members[symbol]
            sector = " / ".join(item["sectors"])
            store.save_state(symbol, item["name"], sector, state)
            store.save_stock_snapshot(captured_at, symbol, item["name"], sector, state, result.provider, result.stale, result.fallback_level)
            summary["symbols_ok"] += 1
            summary["transitions"] += int(state["changed"])
        # 动态池轮换后清理已退出池子的状态，避免旧板块继续污染建议/推送。
        if valid_symbols:
            stale = store.connection.execute(
                "SELECT symbol FROM intraday_states WHERE symbol NOT IN ({})".format(",".join("?" * len(valid_symbols))),
                tuple(sorted(valid_symbols)),
            ).fetchall()
            for row in stale:
                store.connection.execute("DELETE FROM intraday_states WHERE symbol=?", (row[0],))
            store.connection.commit()
        store.prune(90)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="A股5分钟分时影子管线")
    parser.add_argument("--pool", default=str(DEFAULT_POOL))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--ignore-session", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    minute = now.hour * 60 + now.minute
    in_session = now.weekday() < 5 and (570 <= minute <= 690 or 780 <= minute <= 900)
    if not args.ignore_session and not in_session:
        return 0
    print(json.dumps(run(Path(args.pool), Path(args.db), now), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
