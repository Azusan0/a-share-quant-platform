"""信号自评估闭环（#4）。

两个职责：
  1) record_push(): 每次真实推送入场信号时，把 symbol/价格/时间/策略追加到 journal 文件。
     由 monitor 在 emit 成功后调用（低成本、纯本地）。
  2) review(): 事后回看——对 journal 里尚未评估的记录，抓当前价，计算相对推送价的
     T+N 表现，聚合出各策略/各方向的命中率，供复盘调参。可由日报或单独 cron 调用。

命中定义：推送后价格未跌破 -stop_loss（默认 -5%）视为「未坐过山车」，涨幅≥target 视为「命中」。
数据源失败只跳过该条，不影响其余。journal 独立文件，不污染 runtime_state。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def journal_path(config: dict[str, Any]) -> Path:
    """接受完整 config；读 config["signal_journal"]["journal_file"]，与其它增强模块口径一致。

    兼容直接传子配置（含顶层 journal_file）的调用方式。
    """
    cfg = config.get("signal_journal", config) if isinstance(config, dict) else {}
    return Path(cfg.get("journal_file", "/root/.hermes/scripts/a_share_signal_journal.json"))


def record_push(config: dict[str, Any], alert: dict[str, Any]) -> None:
    """把一次入场推送写入 journal（幂等性由 monitor 的冷却/去重保证，这里只追加）。"""
    if alert.get("signal_type") != "entry":
        return
    path = journal_path(config)
    journal = _load_json(path, {"records": []})
    best = alert.get("best_member") or {}
    journal["records"].append({
        "pushed_at": alert.get("time") or datetime.now().isoformat(timespec="seconds"),
        "date": (alert.get("time") or datetime.now().isoformat())[:10],
        "symbol": alert.get("symbol"),
        "name": alert.get("name"),
        "direction": alert.get("direction"),
        "strategy": alert.get("strategy"),
        "strategy_label": alert.get("strategy_label"),
        "push_price": best.get("price") or alert.get("price"),
        "score": alert.get("score"),
        "evaluated": False,
        "outcome": None,
    })
    _save_json(path, journal)


def review(config: dict[str, Any], fetch_snapshot_fn) -> dict[str, Any]:
    """回看未评估记录，计算相对推送价的当前表现并聚合命中率。

    fetch_snapshot_fn: 注入的取价函数（便于离线测试），签名同 data_source.fetch_snapshot。
    """
    path = journal_path(config)
    journal = _load_json(path, {"records": []})
    records = journal.get("records", [])
    strategy = config.get("strategy", {})
    target = float(strategy.get("take_profit_pct", 15.0))
    stop = float(strategy.get("stop_loss_pct", -5.0))
    eval_after_days = int(config.get("journal_eval_after_days", 1))

    now = datetime.now()
    pending = []
    for r in records:
        if r.get("evaluated"):
            continue
        pushed = r.get("pushed_at")
        try:
            pushed_dt = datetime.fromisoformat(pushed)
        except Exception:
            continue
        if now - pushed_dt < timedelta(days=eval_after_days):
            continue  # 还没到评估窗口
        pending.append(r)

    if pending:
        symbols = sorted({r["symbol"] for r in pending if r.get("symbol")})
        try:
            snaps = fetch_snapshot_fn(symbols)
        except Exception:
            snaps = {}
        for r in pending:
            snap = snaps.get(r.get("symbol"))
            push_price = r.get("push_price")
            if not snap or not push_price:
                continue
            cur = float(snap.get("price", 0) or 0)
            if cur <= 0 or push_price <= 0:
                continue
            ret_pct = (cur - push_price) / push_price * 100
            r["return_pct"] = round(ret_pct, 2)
            r["evaluated"] = True
            if ret_pct >= target:
                r["outcome"] = "hit"
            elif ret_pct <= stop:
                r["outcome"] = "stopped"
            else:
                r["outcome"] = "flat"
        _save_json(path, journal)

    # 聚合（含历史已评估记录）
    by_strategy: dict[str, dict[str, int]] = {}
    by_direction: dict[str, dict[str, int]] = {}
    evaluated = [r for r in records if r.get("evaluated") and r.get("outcome")]
    for r in evaluated:
        for bucket, key in ((by_strategy, r.get("strategy_label") or r.get("strategy") or "unknown"),
                            (by_direction, r.get("direction") or "unknown")):
            b = bucket.setdefault(key, {"hit": 0, "flat": 0, "stopped": 0, "total": 0})
            b[r["outcome"]] = b.get(r["outcome"], 0) + 1
            b["total"] += 1

    def _with_rate(d: dict[str, dict[str, int]]) -> dict[str, Any]:
        out = {}
        for k, v in d.items():
            hit_rate = v["hit"] / v["total"] if v["total"] else 0.0
            stop_rate = v["stopped"] / v["total"] if v["total"] else 0.0
            out[k] = {**v, "hit_rate": round(hit_rate, 3), "stop_rate": round(stop_rate, 3)}
        return out

    return {
        "total_records": len(records),
        "evaluated": len(evaluated),
        "by_strategy": _with_rate(by_strategy),
        "by_direction": _with_rate(by_direction),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A股信号自评估")
    # action 可选，默认 review；兼容 `signal_journal.py review --config ...`
    parser.add_argument("action", nargs="?", default="review", choices=["review"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = _load_json(Path(args.config), {})
    from data_source import fetch_snapshot
    summary = review(config, fetch_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
