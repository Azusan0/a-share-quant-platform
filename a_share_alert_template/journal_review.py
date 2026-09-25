#!/usr/bin/env python3
"""按历史交易日回填推送后的 T+1 / T+3 表现。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from data_source import fetch_history


def _atomic_json(path: Path, payload: Any) -> None:
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


def _pct(price: float, base: float) -> float:
    return (price / base - 1) * 100 if base > 0 else 0.0


def evaluate_record(record: dict[str, Any], history: pd.DataFrame, now: datetime) -> bool:
    push_price = float(record.get("push_price") or 0)
    push_date = str(record.get("date") or record.get("pushed_at") or "")[:10]
    if push_price <= 0 or not push_date or history is None or history.empty:
        return False
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    future = frame[frame["date"] > pd.Timestamp(push_date)].head(3)
    # 至少等到下一个完整交易日收盘。
    if future.empty or future.iloc[0]["date"].date() >= now.date():
        return False

    closes = pd.to_numeric(future["close"], errors="coerce").dropna()
    highs = pd.to_numeric(future["high"], errors="coerce").dropna()
    lows = pd.to_numeric(future["low"], errors="coerce").dropna()
    if closes.empty:
        return False
    t1 = _pct(float(closes.iloc[0]), push_price)
    t3 = _pct(float(closes.iloc[min(2, len(closes) - 1)]), push_price)
    mae = _pct(float(lows.min()), push_price) if not lows.empty else min(t1, t3)
    mfe = _pct(float(highs.max()), push_price) if not highs.empty else max(t1, t3)
    record.update({
        "evaluated": True,
        "evaluated_at": now.isoformat(timespec="seconds"),
        "evaluation_method": "historical_t1_t3",
        "t1_return_pct": round(t1, 2),
        "t3_return_pct": round(t3, 2),
        "return_pct": round(t3, 2),
        "mae_pct": round(mae, 2),
        "mfe_pct": round(mfe, 2),
        "outcome": "hit" if t3 >= 3 else "stopped" if mae <= -5 else "flat",
    })
    return True


def review(path: Path, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now()
    journal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": []}
    records = journal.get("records", [])
    pending_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not record.get("evaluated") and record.get("symbol"):
            pending_by_symbol.setdefault(str(record["symbol"]), []).append(record)
    updated = 0
    for symbol, pending in pending_by_symbol.items():
        try:
            history = fetch_history(symbol, 120)
        except Exception:
            continue
        for record in pending:
            updated += int(evaluate_record(record, history, now))
    if updated:
        _atomic_json(path, journal)
    return {"records": len(records), "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="回填信号 T+1/T+3 表现")
    parser.add_argument("--journal", default="/root/.hermes/scripts/a_share_signal_journal.json")
    args = parser.parse_args()
    print(json.dumps(review(Path(args.journal)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
