from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def emit_alert(alert: dict[str, Any], delivery: dict[str, Any]) -> None:
    mode = delivery.get("mode", "stdout")
    if mode == "stdout":
        _emit_stdout(alert)
        return
    if mode == "jsonl":
        _emit_jsonl(alert, delivery)
        return
    raise ValueError(f"unsupported delivery mode: {mode}")


def _emit_stdout(alert: dict[str, Any]) -> None:
    print(json.dumps(alert, ensure_ascii=False), flush=True)


def _emit_jsonl(alert: dict[str, Any], delivery: dict[str, Any]) -> None:
    output_path = Path(delivery.get("jsonl_path", "a_share_alert_template/state/alerts.jsonl"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")
