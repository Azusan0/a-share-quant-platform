# -*- coding: utf-8 -*-
"""半自动带护栏迭代器（#自我迭代）。

定位：这是「AI 辅助、人做决策」的迭代闭环，**只产出建议提案，绝不自动改 config**。
你审阅提案后，手动把认可的改动落到 config，再重跑回测确认——护栏始终在你手上。

三件事：
  1) 跑回测（复用 backtest.run_backtest），量化每个方向/子策略的真实边缘（期望值/胜率/盈亏比/样本）。
  2) 读实盘流水（signal_journal），对比「回测基准 vs 实盘真实表现」，检测策略是否在衰减/漂移。
  3) 生成结构化提案：
       - 方向层：负期望且样本足→建议剔除；边缘偏弱→建议降权；强正期望→建议保留/加权。
       - 策略层：某子策略期望为负或远低于其它→建议收紧其阈值或停用。
       - 仓位层：按各方向期望值×盈亏比给出相对仓位权重建议（Kelly 简化版，带上限）。
       - 漂移层：实盘期望显著差于回测→提示该方向/策略可能失效，优先复核。

用法：
  python iterator.py --config <config> --days 500 [--out proposal.json]
输出：人类可读的中文提案 + 结构化 JSON（--out）。所有阈值判定可在 config.iterator 覆盖。

安全底线：任何数据缺失/回测失败都跳过对应建议，不臆造；样本不足的方向明确标注「样本不足，暂不下结论」。
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from backtest import run_backtest


# ---------- 判定阈值（可被 config.iterator 覆盖） ----------
DEFAULTS = {
    "min_samples": 30,          # 样本数低于此值不下硬结论（只提示）
    "drop_expectancy": -0.5,    # 期望值(%/笔)低于此且样本足 → 建议剔除
    "downweight_expectancy": 0.3,  # 期望值低于此(但高于drop) → 建议降权
    "strong_expectancy": 1.5,   # 期望值高于此 → 强方向，建议保留/加权
    "weak_strategy_expectancy": 0.3,  # 子策略期望低于此 → 建议收紧/停用
    "drift_ratio": 0.5,         # 实盘期望 < 回测期望 * 此比例 → 判为衰减
    "drift_min_live_samples": 8,  # 实盘样本少于此不做漂移判定（噪声太大）
    "max_position_weight": 0.30,  # 单方向仓位权重上限
    "backtest_days": 500,
}


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(config.get("iterator", {}) or {})
    return merged


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


# ---------- 实盘流水读取（对齐 signal_journal 的结构） ----------
def _load_live_journal(config: dict[str, Any]) -> list[dict[str, Any]]:
    jcfg = config.get("signal_journal", {}) if isinstance(config, dict) else {}
    path = Path(jcfg.get("journal_file", "/root/.hermes/scripts/a_share_signal_journal.json"))
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data.get("records", []) if isinstance(data, dict) else []


def _live_expectancy_by(records: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """按 key(direction/strategy_label) 聚合实盘已评估记录的期望值。

    只统计 evaluated 且有 return_pct 的记录，避免用未结算样本。
    """
    groups: dict[str, list[float]] = {}
    for r in records:
        if not r.get("evaluated"):
            continue
        ret = _num(r.get("return_pct"))
        if ret is None:
            continue
        k = str(r.get(key) or "unknown")
        groups.setdefault(k, []).append(ret)
    out: dict[str, dict[str, Any]] = {}
    for k, rets in groups.items():
        n = len(rets)
        if n == 0:
            continue
        exp = sum(rets) / n
        wins = sum(1 for x in rets if x > 0)
        out[k] = {"live_trades": n, "live_expectancy": round(exp, 3), "live_win_rate": round(wins / n, 3)}
    return out


# ---------- 提案生成 ----------
def _direction_proposals(by_direction: dict[str, Any], live: dict[str, dict[str, Any]], p: dict[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for direction, m in by_direction.items():
        trades = int(m.get("trades", 0) or 0)
        exp = _num(m.get("expectancy_pct"))
        payoff = _num(m.get("payoff_ratio"))
        win = _num(m.get("win_rate"))
        if exp is None:
            continue

        item: dict[str, Any] = {
            "direction": direction,
            "backtest_trades": trades,
            "backtest_expectancy": exp,
            "backtest_win_rate": win,
            "backtest_payoff": payoff,
        }

        # 样本不足：只提示，不下硬结论
        if trades < int(p["min_samples"]):
            item["action"] = "hold_insufficient_sample"
            item["reason"] = f"样本仅{trades}笔(<{p['min_samples']})，统计不显著，暂不调整，继续观察"
            proposals.append(item)
            continue

        if exp <= float(p["drop_expectancy"]):
            item["action"] = "drop"
            item["reason"] = f"期望值{exp:+.2f}%/笔为明显负边缘(样本{trades})，建议从方向池剔除或移入观察不推送"
        elif exp <= float(p["downweight_expectancy"]):
            item["action"] = "downweight"
            item["reason"] = f"期望值{exp:+.2f}%/笔偏弱，建议降低仓位权重、只做高分信号"
        elif exp >= float(p["strong_expectancy"]):
            item["action"] = "keep_or_boost"
            item["reason"] = f"期望值{exp:+.2f}%/笔、盈亏比{payoff}为强方向，建议保留并可适度加权"
        else:
            item["action"] = "keep"
            item["reason"] = f"期望值{exp:+.2f}%/笔为正但一般，维持现状"

        # 漂移检测：实盘 vs 回测
        lv = live.get(direction)
        if lv and lv["live_trades"] >= int(p["drift_min_live_samples"]):
            item["live_trades"] = lv["live_trades"]
            item["live_expectancy"] = lv["live_expectancy"]
            live_exp = lv["live_expectancy"]
            if exp > 0 and live_exp < exp * float(p["drift_ratio"]):
                item["drift_warning"] = (
                    f"实盘期望{live_exp:+.2f}% 显著低于回测{exp:+.2f}%，该方向可能在衰减，"
                    f"建议优先复核（是否风格切换/信号失效）"
                )
        proposals.append(item)

    proposals.sort(key=lambda x: (x.get("backtest_expectancy") or 0), reverse=True)
    return proposals


def _strategy_proposals(by_strategy: dict[str, Any], live: dict[str, dict[str, Any]], p: dict[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for label, m in by_strategy.items():
        trades = int(m.get("trades", 0) or 0)
        exp = _num(m.get("expectancy_pct"))
        if exp is None:
            continue
        item: dict[str, Any] = {
            "strategy_label": label,
            "backtest_trades": trades,
            "backtest_expectancy": exp,
        }
        if trades < int(p["min_samples"]):
            item["action"] = "hold_insufficient_sample"
            item["reason"] = f"样本仅{trades}笔，暂不调整"
        elif exp <= float(p["weak_strategy_expectancy"]):
            item["action"] = "tighten_or_disable"
            item["reason"] = (
                f"该子策略期望{exp:+.2f}%/笔偏弱，建议收紧其入场阈值"
                f"（如提高对应 min_change/min_score/放量要求），或在 config 中停用"
            )
        else:
            item["action"] = "keep"
            item["reason"] = f"期望{exp:+.2f}%/笔可用，维持"
        lv = live.get(label)
        if lv and lv["live_trades"] >= int(p["drift_min_live_samples"]):
            item["live_trades"] = lv["live_trades"]
            item["live_expectancy"] = lv["live_expectancy"]
        proposals.append(item)
    proposals.sort(key=lambda x: (x.get("backtest_expectancy") or 0), reverse=True)
    return proposals


def _position_weights(by_direction: dict[str, Any], p: dict[str, Any]) -> list[dict[str, Any]]:
    """按期望值×盈亏比给出相对仓位权重建议（简化 Kelly，样本不足或负期望权重为0）。

    注意：这是相对权重参考，不是精确 Kelly，也不构成投资建议——用于「强方向多分、弱方向少分」的纪律化分配。
    """
    raw: dict[str, float] = {}
    for direction, m in by_direction.items():
        trades = int(m.get("trades", 0) or 0)
        exp = _num(m.get("expectancy_pct"))
        payoff = _num(m.get("payoff_ratio"))
        win = _num(m.get("win_rate"))
        if exp is None or trades < int(p["min_samples"]) or exp <= 0 or not payoff or payoff <= 0 or win is None:
            raw[direction] = 0.0
            continue
        # 简化 Kelly：f = win - (1-win)/payoff，截断到 [0, +∞)
        kelly = win - (1 - win) / payoff
        raw[direction] = max(0.0, kelly)

    total = sum(raw.values())
    cap = float(p["max_position_weight"])
    weights: list[dict[str, Any]] = []
    if total <= 0:
        for d in raw:
            weights.append({"direction": d, "suggested_weight": 0.0, "note": "非正边缘或样本不足，建议不配仓"})
        return weights

    qualifying = {d: v for d, v in raw.items() if v > 0}
    n_qual = len(qualifying)
    # 若正边缘方向太少，单方向上限在数学上无法与"归一化到100%"并存
    # （n_qual * cap < 1）。此时放弃硬上限，按 Kelly 比例分配，并标注集中度偏高。
    concentration_note = n_qual * cap < 1.0 - 1e-9
    q_total = sum(qualifying.values())
    base = {d: v / q_total for d, v in qualifying.items()}  # 归一化的 Kelly 占比

    if concentration_note:
        final = base
    else:
        # 迭代式注水截断：反复把超过 cap 的方向压到 cap，把余量按比例分给未达上限者，
        # 直到所有权重都 <= cap 且总和为 1。
        final = dict(base)
        for _ in range(20):
            over = {d: w for d, w in final.items() if w > cap + 1e-9}
            if not over:
                break
            excess = sum(w - cap for w in over.values())
            for d in over:
                final[d] = cap
            under = {d: w for d, w in final.items() if w < cap - 1e-9}
            under_total = sum(under.values())
            if under_total <= 0:
                break
            for d in under:
                final[d] += excess * (final[d] / under_total)

    for d in raw:
        if d not in final:
            weights.append({"direction": d, "suggested_weight": 0.0, "note": "非正边缘或样本不足，建议不配仓"})
    for d, v in sorted(final.items(), key=lambda x: x[1], reverse=True):
        item = {"direction": d, "suggested_weight": round(v, 3)}
        if concentration_note:
            item["note"] = f"可选正边缘方向仅{n_qual}个，单方向权重超过{cap:.0%}上限，集中度偏高，注意风险"
        weights.append(item)
    return weights


def generate_proposal(config: dict[str, Any], days: int | None = None) -> dict[str, Any]:
    p = _cfg(config)
    days = int(days or p["backtest_days"])

    bt = run_backtest(config, days)
    by_direction = {k: v for k, v in bt.get("by_direction", {}).items() if isinstance(v, dict) and v.get("trades", 0)}
    by_strategy = {k: v for k, v in bt.get("by_strategy", {}).items() if isinstance(v, dict) and v.get("trades", 0)}

    live_records = _load_live_journal(config)
    live_by_dir = _live_expectancy_by(live_records, "direction")
    live_by_strat = _live_expectancy_by(live_records, "strategy_label")

    overall = bt.get("overall", {})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backtest_window_days": days,
        "backtest_overall": overall,
        "live_records_total": len(live_records),
        "direction_proposals": _direction_proposals(by_direction, live_by_dir, p),
        "strategy_proposals": _strategy_proposals(by_strategy, live_by_strat, p),
        "position_weight_suggestions": _position_weights(by_direction, p),
        "disclaimer": (
            "本提案由历史回测+实盘流水统计生成，仅为纪律化调参参考，不构成投资建议。"
            "回测基于单一历史区间，未来市场风格可能切换；请人工审阅后再决定是否落到 config，"
            "并在改动后重跑回测确认期望值确实变好。"
        ),
    }


def _print_human(proposal: dict[str, Any]) -> None:
    o = proposal.get("backtest_overall", {})
    print("=" * 60)
    print(f"迭代提案 | 生成于 {proposal['generated_at']} | 回测窗口 {proposal['backtest_window_days']} 交易日")
    if o:
        print(f"整体：{o.get('trades')}笔 胜率{(o.get('win_rate') or 0)*100:.0f}% "
              f"盈亏比{o.get('payoff_ratio')} 期望值{o.get('expectancy_pct'):+}%/笔")
    print(f"实盘流水记录数：{proposal['live_records_total']}")

    print("\n【方向层建议】(按回测期望值排序)")
    for it in proposal["direction_proposals"]:
        line = f"  {it['direction']:<8} [{it['action']}] {it['reason']}"
        print(line)
        if it.get("drift_warning"):
            print(f"      ⚠ {it['drift_warning']}")

    print("\n【策略层建议】")
    for it in proposal["strategy_proposals"]:
        print(f"  {it['strategy_label']:<8} [{it['action']}] {it['reason']}")

    print("\n【仓位权重建议】(相对参考，非投资建议)")
    for it in proposal["position_weight_suggestions"]:
        w = it["suggested_weight"]
        note = f"  {it.get('note','')}" if it.get("note") else ""
        print(f"  {it['direction']:<8} 权重 {w:.1%}{note}")

    print("\n" + proposal["disclaimer"])
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股策略半自动迭代器（只产出建议，不自动改config）")
    parser.add_argument("--config", required=True)
    parser.add_argument("--days", type=int, default=0, help="回测窗口交易日数(默认取config.iterator.backtest_days或500)")
    parser.add_argument("--out", default="", help="结构化提案写入的 JSON 文件路径")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    proposal = generate_proposal(config, args.days or None)
    _print_human(proposal)
    if args.out:
        Path(args.out).write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结构化提案已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
