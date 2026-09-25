#!/usr/bin/env python3
"""全市场宽度与情绪快照。

参考 go-stock 的市场统计思路重新实现，不复制 GPL 代码。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


URL = "https://x-quote.cls.cn/quote/index/home?app=CailianpressWeb&os=web&sv=8.4.6"
DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_market_sentiment.json")


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


def classify(up_ratio: float, limit_up: int, limit_down: int, average_rise: float) -> tuple[str, float]:
    score = up_ratio * 0.55
    score += min(max(average_rise + 1.5, 0) / 3 * 20, 20)
    score += min(max(limit_up - limit_down, -20) + 20, 40) / 40 * 25
    score = min(max(score, 0), 100)
    if score >= 75:
        level = "risk_on"
        label = "强势"
    elif score >= 55:
        level = "positive"
        label = "偏强"
    elif score >= 40:
        level = "neutral"
        label = "中性"
    elif score >= 25:
        level = "weak"
        label = "偏弱"
    else:
        level = "risk_off"
        label = "冰点"
    return f"{level}:{label}", round(score, 2)


def fetch_sentiment() -> dict[str, Any]:
    response = requests.get(URL, timeout=12, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.cls.cn/",
    })
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", 0)) != 200:
        raise RuntimeError(f"CLS market statistic error: {payload.get('code')} {payload.get('msg')}")
    data = payload.get("data") or {}
    distribution = data.get("up_down_dis") or {}
    up = int(_num(distribution.get("rise_num")))
    down = int(_num(distribution.get("fall_num")))
    flat = int(_num(distribution.get("flat_num")))
    limit_up = int(_num(distribution.get("up_num")))
    limit_down = int(_num(distribution.get("down_num")))
    average_rise = _num(distribution.get("average_rise"))
    # CLS 返回小数比例，例如 0.028 表示 2.8%。
    if abs(average_rise) <= 0.2:
        average_rise *= 100
    total = up + down + flat
    up_ratio = up / total * 100 if total else 0
    tagged, score = classify(up_ratio, limit_up, limit_down, average_rise)
    level, label = tagged.split(":", 1)
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": "cls",
        "level": level,
        "label": label,
        "score": score,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio_pct": round(up_ratio, 2),
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "average_rise_pct": round(average_rise, 3),
        "distribution": {
            key: int(_num(distribution.get(key)))
            for key in ("down_10", "down_8", "down_6", "down_4", "down_2", "up_2", "up_4", "up_6", "up_8", "up_10")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场情绪快照")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    _atomic_json(Path(args.output), fetch_sentiment())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
