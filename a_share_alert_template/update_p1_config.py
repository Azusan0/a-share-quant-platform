#!/usr/bin/env python3
"""幂等补充P1推荐引擎配置，不读取或输出任何密钥。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


DEFAULTS = {
    "mode": "shadow",
    "snapshot_file": "/root/.hermes/scripts/a_share_recommendation_snapshot.json",
    "db_file": "/root/.hermes/scripts/a_share_market_snapshots.db",
    "max_input_age_minutes": 12,
    "max_state_age_minutes": 12,
    "max_snapshot_age_minutes": 10,
    "risk_reward_min": 2.0,
    "max_target_up_pct": 15,
    "retention_days": 90,
    "portfolio_guard": {
        "max_active_total": 3,
        "max_active_per_sector": 1,
        "max_new_per_day": 5,
        "min_recommendation_score": 50,
        "risk_on_max_active": 3,
        "positive_max_active": 2,
        "neutral_max_active": 1,
        "weak_max_active": 0,
        "risk_off_max_active": 0,
    },
}


def update(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    current = config.setdefault("recommendation_engine", {})
    for key, value in DEFAULTS.items():
        if key == "portfolio_guard":
            guard = current.setdefault(key, {})
            for guard_key, guard_value in value.items():
                guard.setdefault(guard_key, guard_value)
        else:
            current.setdefault(key, value)
    # 安全约束：升级脚本永远不自动开启正式推送。
    current["mode"] = "shadow"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    update(Path(args.config))
