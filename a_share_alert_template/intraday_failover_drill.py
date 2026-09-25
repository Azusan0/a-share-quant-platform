#!/usr/bin/env python3
"""分钟行情真实双源容灾演练，不影响正式行情和推荐状态。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from intraday_data import FetchResult, fetch_m5, fetch_sina_m5


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_failover_drill.json")


def compare_results(primary: FetchResult, fallback: FetchResult) -> dict[str, Any]:
    primary_bars = {row["time"]: row for row in primary.bars}
    fallback_bars = {row["time"]: row for row in fallback.bars}
    common = sorted(set(primary_bars) & set(fallback_bars))
    close_diffs: list[float] = []
    volume_diffs: list[float] = []
    for time in common:
        left, right = primary_bars[time], fallback_bars[time]
        base_close = max(float(left["close"]), 1e-9)
        close_diffs.append(abs(float(left["close"]) - float(right["close"])) / base_close * 100)
        base_volume = max(float(left["volume_shares"]), 1)
        volume_diffs.append(abs(float(left["volume_shares"]) - float(right["volume_shares"])) / base_volume * 100)
    try:
        latest_gap = abs((datetime.fromisoformat(primary.bars[-1]["time"]) - datetime.fromisoformat(fallback.bars[-1]["time"])).total_seconds()) / 60
    except Exception:
        latest_gap = 9999.0
    max_close_diff = max(close_diffs) if close_diffs else None
    passed = len(fallback.bars) >= 6 and len(common) >= 6 and latest_gap <= 5 and max_close_diff is not None and max_close_diff <= .5
    return {
        "passed": passed, "mode": "simulated_primary_failure_real_fallback",
        "selected_provider": fallback.provider, "fallback_level": 1,
        "primary_provider": primary.provider, "primary_bars": len(primary.bars),
        "fallback_provider": fallback.provider, "fallback_bars": len(fallback.bars),
        "common_bars": len(common), "latest_time_gap_minutes": round(latest_gap, 2),
        "max_close_diff_pct": round(max_close_diff, 4) if max_close_diff is not None else None,
        "median_close_diff_pct": round(median(close_diffs), 4) if close_diffs else None,
        "median_volume_diff_pct": round(median(volume_diffs), 2) if volume_diffs else None,
        "primary_stale": primary.stale, "fallback_stale": fallback.stale,
        "primary_latest": primary.bars[-1]["time"] if primary.bars else None,
        "fallback_latest": fallback.bars[-1]["time"] if fallback.bars else None,
    }


def run(symbol: str, limit: int = 80) -> dict[str, Any]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    result: dict[str, Any] = {"generated_at": generated_at, "symbol": symbol, "passed": False}
    try:
        primary = fetch_m5(symbol, limit=limit, retries=1)
        result["primary"] = {"provider": primary.provider, "bars": len(primary.bars), "stale": primary.stale}
    except Exception as exc:
        result["error"] = f"主源基线失败: {type(exc).__name__}: {str(exc)[:300]}"
        return result
    try:
        fallback = fetch_sina_m5(symbol, limit=limit, retries=1)
        result.update(compare_results(primary, fallback))
    except Exception as exc:
        result["error"] = f"备用源接管失败: {type(exc).__name__}: {str(exc)[:300]}"
    return result


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="分钟行情真实双源容灾演练")
    parser.add_argument("--symbol", default="600000")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    payload = run(args.symbol, max(10, min(args.limit, 320)))
    _atomic_json(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
