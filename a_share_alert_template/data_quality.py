#!/usr/bin/env python3
"""推荐数据质量聚合：把数据完整度与交易阻断原因分开。"""
from __future__ import annotations

from typing import Any


QUALITY_ORDER = {"missing": 0, "stale": 1, "partial": 2, "complete": 3}


def worst_quality(values: list[str]) -> str:
    if not values:
        return "missing"
    return min((str(value or "missing") for value in values), key=lambda item: QUALITY_ORDER.get(item, 0))


def quality_from_bool(ok: bool, missing_label: str = "missing") -> str:
    return "complete" if ok else missing_label


def recommendation_quality(row: dict[str, Any], diagnosis: dict[str, Any], *, price: float, vwap: float,
                           volume_ratio: float, now: Any = None) -> dict[str, Any]:
    required = {
        "quote": quality_from_bool(price > 0 and vwap > 0),
        "volume": quality_from_bool(volume_ratio > 0),
        "technical": str(diagnosis.get("data_quality") or "missing"),
    }
    overall = worst_quality(list(required.values()))
    missing: list[str] = []
    if required["quote"] != "complete":
        missing.append("实时价格或VWAP缺失")
    if required["volume"] != "complete":
        missing.append("同期量能缺失")
    if required["technical"] in {"missing", "stale"}:
        missing.extend(diagnosis.get("missing_data") or ["多周期技术诊断缺失"])
    return {
        "overall": overall,
        "actionable": overall in {"complete", "partial"},
        "required": required,
        "missing_data": list(dict.fromkeys(str(item) for item in missing if item)),
    }
