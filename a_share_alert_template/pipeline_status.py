#!/usr/bin/env python3
"""原子维护影子流水线运行清单。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path("/root/.hermes/scripts/a_share_pipeline_manifest.json")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic(path: Path, payload: dict[str, Any]) -> None:
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


def start_run(path: Path, run_id: str) -> None:
    _atomic(path, {"run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"),
                   "finished_at": None, "status": "running", "critical_failure": False, "stages": []})


def finish_stage(path: Path, name: str, status: str, duration_seconds: int, critical: bool,
                 error_summary: str = "") -> None:
    payload = _load(path)
    payload.setdefault("stages", []).append({
        "name": name, "status": status, "critical": critical,
        "duration_seconds": int(duration_seconds),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "error_summary": str(error_summary)[-800:],
    })
    if critical and status != "ok":
        payload["critical_failure"] = True
        payload["status"] = "failed"
    elif payload.get("status") != "failed" and status != "ok":
        payload["status"] = "degraded"
    _atomic(path, payload)


def finish_run(path: Path) -> None:
    payload = _load(path)
    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if payload.get("status") == "running":
        payload["status"] = "ok"
    _atomic(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start"); start.add_argument("run_id")
    stage = sub.add_parser("stage")
    stage.add_argument("name"); stage.add_argument("status"); stage.add_argument("duration", type=int)
    stage.add_argument("--critical", action="store_true"); stage.add_argument("--error", default="")
    sub.add_parser("finish")
    args = parser.parse_args()
    path = Path(args.manifest)
    if args.command == "start":
        start_run(path, args.run_id)
    elif args.command == "stage":
        finish_stage(path, args.name, args.status, args.duration, args.critical, args.error)
    else:
        finish_run(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
