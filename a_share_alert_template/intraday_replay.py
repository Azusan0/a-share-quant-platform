#!/usr/bin/env python3
"""历史5分钟K离线回放与降级一致性审计。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from intraday_state import evaluate_state
from snapshot_store import DEFAULT_DB, SnapshotStore


def session_slots(trade_date: str) -> list[str]:
    day = datetime.strptime(trade_date, "%Y-%m-%d")
    ranges = ((day.replace(hour=9, minute=35), day.replace(hour=11, minute=30)),
              (day.replace(hour=13, minute=5), day.replace(hour=15, minute=0)))
    result: list[str] = []
    for start, end in ranges:
        current = start
        while current <= end:
            result.append(current.isoformat(timespec="minutes"))
            current += timedelta(minutes=5)
    return result


def _valid_bar(row: dict[str, Any], trade_date: str) -> bool:
    try:
        time = datetime.fromisoformat(str(row["time"]))
        open_price, close, high, low = (float(row[key]) for key in ("open", "close", "high", "low"))
        volume, amount = float(row["volume_shares"]), float(row["amount_estimated"])
    except (KeyError, TypeError, ValueError):
        return False
    return (time.date().isoformat() == trade_date and min(open_price, close, high, low) > 0
            and high >= max(open_price, close, low) and low <= min(open_price, close, high)
            and volume >= 0 and amount >= 0)


def _replay(bars: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[tuple[str, str]], bool]:
    previous: dict[str, Any] | None = None
    transitions: list[tuple[str, str]] = []
    idempotent = True
    for size in range(6, len(bars) + 1):
        current = evaluate_state(bars[:size], previous)
        repeated = evaluate_state(bars[:size], current)
        stable_keys = ("state", "entered_at", "bars_in_state", "bar_time")
        if any(current.get(key) != repeated.get(key) for key in stable_keys) or repeated.get("changed"):
            idempotent = False
        if current.get("changed"):
            transitions.append((str(current.get("bar_time")), str(current.get("state"))))
        previous = current
    return previous, transitions, idempotent


def audit_symbol(symbol: str, trade_date: str, bars: Iterable[dict[str, Any]]) -> dict[str, Any]:
    raw = sorted((dict(row) for row in bars), key=lambda row: str(row.get("time") or ""))
    counts = Counter(str(row.get("time") or "") for row in raw)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    invalid_count = sum(not _valid_bar(row, trade_date) for row in raw)
    valid = [row for row in raw if _valid_bar(row, trade_date)]
    unique = {str(row["time"]): row for row in valid}
    valid = [unique[key] for key in sorted(unique)]
    expected = session_slots(trade_date)
    missing = [slot for slot in expected if slot not in unique]
    coverage = len(set(expected) & set(unique)) / len(expected) * 100

    baseline, transitions, idempotent = _replay(valid) if len(valid) >= 6 else (None, [], False)
    fallback_bars = [dict(row, provider="sina_m5" if index >= len(valid) // 2 else row.get("provider", "tencent_m5"))
                     for index, row in enumerate(valid)]
    fallback, fallback_transitions, fallback_idempotent = _replay(fallback_bars) if len(valid) >= 6 else (None, [], False)
    fallback_consistent = bool(baseline and fallback and baseline.get("state") == fallback.get("state")
                               and transitions == fallback_transitions)
    idempotent = idempotent and fallback_idempotent
    if invalid_count > 0 or duplicate_count > 0:
        quality = "invalid"
    elif coverage >= 95 and fallback_consistent and idempotent:
        quality = "complete"
    elif coverage >= 50 and len(valid) >= 6 and invalid_count <= 2 and fallback_consistent and idempotent:
        quality = "partial"
    else:
        quality = "insufficient"
    return {
        "symbol": symbol, "trade_date": trade_date, "quality": quality,
        "bars": len(valid), "expected_bars": len(expected), "coverage_pct": round(coverage, 2),
        "missing_count": len(missing), "invalid_count": invalid_count,
        "duplicate_count": duplicate_count, "fallback_consistent": fallback_consistent,
        "idempotent": idempotent, "final_state": baseline.get("state") if baseline else None,
        "transition_count": len(transitions), "provider_counts": dict(Counter(str(row.get("provider") or "unknown") for row in valid)),
        "missing_times": missing, "transitions": [{"time": time, "state": state} for time, state in transitions],
    }


def run(db_path: Path, trade_date: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    if not trade_date or trade_date == "latest":
        row = connection.execute("SELECT MAX(trade_date) FROM intraday_bars").fetchone()
        trade_date = str(row[0] or "")
    if not trade_date:
        connection.close()
        raise RuntimeError("分钟K数据库中没有可回放交易日")
    params: list[Any] = [trade_date]
    where = "trade_date=?"
    if symbols:
        where += f" AND symbol IN ({','.join('?' for _ in symbols)})"
        params.extend(symbols)
    rows = connection.execute(f"""
      SELECT symbol,bar_time AS time,open,close,high,low,volume_shares,amount_estimated,provider
      FROM intraday_bars WHERE {where} ORDER BY symbol,bar_time
    """, params).fetchall()
    connection.close()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(dict(row))
    results = [audit_symbol(symbol, trade_date, bars) for symbol, bars in sorted(grouped.items())]
    generated_at = datetime.now().isoformat(timespec="seconds")
    with SnapshotStore(db_path) as store:
        store.save_intraday_replays(generated_at, results)
        store.prune(90)
    counts = Counter(row["quality"] for row in results)
    return {
        "generated_at": generated_at, "trade_date": trade_date, "symbols": len(results),
        "summary": {"complete": counts["complete"], "partial": counts["partial"],
                     "insufficient": counts["insufficient"], "invalid": counts["invalid"]},
        "fallback_consistent": sum(bool(row["fallback_consistent"]) for row in results),
        "idempotent": sum(bool(row["idempotent"]) for row in results), "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="历史5分钟K离线降级回放")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--date", default="latest")
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = run(Path(args.db), args.date, args.symbol)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
