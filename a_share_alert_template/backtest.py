"""回测引擎（量化地基）。

目的：把 strategies.py 里的策略函数拿到历史数据上逐日重放，量化出真实边缘——
胜率、盈亏比、期望值、最大回撤——让「阈值到底赚不赚钱」有据可依，而不是拍脑袋。

核心设计（贴合 A 股现实，避免虚高收益）：
  1) 逐日重放：对第 i 个交易日，指标只用「前 i 根已收盘 K 线」，当日用第 i 根 bar
     重构成 snapshot，喂给 evaluate_signal —— 与实盘 monitor「指标用已收盘、当日用快照」
     口径完全一致，杜绝未来函数（look-ahead bias）。
  2) T+1 成交：A 股当日买入次日才能卖。信号在第 i 日收盘触发 → 第 i+1 日**开盘价**买入。
     若次日开盘一字涨停（open==high 且高开幅度≥涨停阈值），视为买不进，跳过该信号。
  3) 离场模拟：买入后逐日检查，触发止损/止盈/最大持有天数则按当日价离场；
     止损止盈同日触发时，保守假设先触发止损（悲观），避免高估收益。
  4) 交易成本：买卖双边佣金 + 卖出印花税 + 滑点，全部扣除，得到净收益。

用法：
  python backtest.py --config <config> --days 250            # 全池回测最近250交易日
  python backtest.py --config <config> --symbol 603019 --days 500
输出：整体 + 分策略 + 分方向的量化指标 JSON。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from data_source import fetch_history, _is_etf
from strategies import evaluate_signal


# ---------- 成交与成本假设（可被 config.backtest 覆盖） ----------
DEFAULTS = {
    "hold_max_days": 10,        # 最长持有交易日数，到期强制离场
    "stop_loss_pct": -5.0,      # 止损线（相对买入价）
    "take_profit_pct": 15.0,    # 止盈线
    "commission_pct": 0.025,    # 单边佣金 %（万2.5，含最低5元这里简化）
    "stamp_tax_pct": 0.05,      # 印花税 %（仅卖出，2023年后千0.5）
    "slippage_pct": 0.10,       # 单边滑点 %（保守）
    "limit_up_pct_stock": 9.5,  # 个股一字涨停判定：高开≥此值且 open==high 视为买不进
    "limit_up_pct_etf": 4.5,
    "min_history_bars": 30,     # 触发评估所需的最少已收盘 bar
}


def _cfg(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(config.get("backtest", {}) or {})
    # 止损止盈优先取 strategy 段，保证回测与实盘同参
    strat = config.get("strategy", {}) or {}
    for k in ("stop_loss_pct", "take_profit_pct"):
        if k in strat:
            merged[k] = strat[k]
    return merged


def _first_valid_amount(row: pd.Series, close: float) -> float:
    """取当日成交额(元)。优先用真实 amount；缺失则用真实 volume(股)反推 amount=volume*close，
    保证下游 amount/price 恢复成 volume，与历史 avg_volume(股) 同口径。两者都缺才返回 0。
    """
    raw = row.get("amount")
    try:
        val = float(raw)
        if not math.isnan(val) and val > 0:
            return val
    except (TypeError, ValueError):
        pass
    vol = row.get("volume")
    try:
        v = float(vol)
        if not math.isnan(v) and v > 0 and close > 0:
            return v * close
    except (TypeError, ValueError):
        pass
    return 0.0


def _row_to_snapshot(row: pd.Series, prev_close: float, symbol: str, name: str) -> dict[str, Any] | None:
    """把一根历史日线 bar 重构成 evaluate_signal 需要的 snapshot（当日口径）。"""
    try:
        close = float(row["close"])
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
    except (TypeError, ValueError, KeyError):
        return None
    if any(math.isnan(x) or x <= 0 for x in (close, open_, high, low)):
        return None
    if prev_close <= 0 or math.isnan(prev_close):
        return None
    change_pct = (close - prev_close) / prev_close * 100
    # 关键：evaluate_signal 内部用 amount/price 反推「当日成交量(股)」，再与历史 avg_volume(股) 比。
    # 但多数历史源(腾讯/新浪，以及东财被限流回退时)没有真实成交额，amount 常年 NaN。
    # 若把 NaN amount 当 0，量比恒为 0 —— 右侧突破/回踩反包全被量能门槛挡掉，只剩回踩低吸，
    # 结论严重失真。这里改为：amount 缺失时用真实 volume(股) 反推 amount = volume*close，
    # 使 amount/price == volume，与历史 avg_volume 同口径；volume 也缺才置 0(策略会判为量能缺失)。
    amount = _first_valid_amount(row, close)
    return {
        "symbol": symbol,
        "name": name,
        "price": close,          # 回测用当日收盘作为"现价"
        "open": open_,
        "high": high,
        "low": low,
        "change_pct": round(change_pct, 3),
        "amount": amount,
    }


def _simulate_trade(
    future: pd.DataFrame,
    params: dict[str, Any],
    is_etf: bool,
    trigger_close: float,
) -> dict[str, Any] | None:
    """给定信号触发日之后的 K 线，模拟 T+1 开盘买入到离场的一笔交易。

    future: 触发日之后的所有 bar（future.iloc[0] 即次日）。
    trigger_close: 触发日收盘价，用于判次日高开幅度（一字涨停买不进）。
    返回该笔交易结果或 None（数据不足），买不进时返回 {"skipped": ...}。
    """
    if future is None or len(future) < 1:
        return None
    entry_bar = future.iloc[0]
    try:
        entry_open = float(entry_bar["open"])
        entry_high = float(entry_bar["high"])
    except (TypeError, ValueError, KeyError):
        return None
    if entry_open <= 0 or math.isnan(entry_open):
        return None

    # 次日一字涨停买不进：高开幅度≥涨停阈值 且 开盘即最高（开=高，全天没给更低价）
    # → 现实中挂单排不上、无法在开盘价成交，跳过该信号（避免高估收益）。
    limit_pct = params["limit_up_pct_etf"] if is_etf else params["limit_up_pct_stock"]
    if trigger_close > 0 and not math.isnan(trigger_close):
        gap_up_pct = (entry_open - trigger_close) / trigger_close * 100
        if gap_up_pct >= limit_pct and abs(entry_high - entry_open) < 1e-9:
            return {"skipped": "limit_up_unfillable"}

    slip = params["slippage_pct"] / 100
    buy_price = entry_open * (1 + slip)  # 买入含滑点
    stop_line = buy_price * (1 + params["stop_loss_pct"] / 100)
    take_line = buy_price * (1 + params["take_profit_pct"] / 100)
    hold_max = int(params["hold_max_days"])

    exit_price = None
    exit_reason = None
    hold_days = 0
    # 从买入当日（future.iloc[0]）起逐日检查；买入日当天也可能触发止损/止盈
    for i in range(min(hold_max, len(future))):
        bar = future.iloc[i]
        hold_days = i + 1
        try:
            hi = float(bar["high"])
            lo = float(bar["low"])
            cl = float(bar["close"])
        except (TypeError, ValueError, KeyError):
            continue
        # 悲观假设：同日先判止损，再判止盈
        if lo <= stop_line:
            exit_price = stop_line
            exit_reason = "stop_loss"
            break
        if hi >= take_line:
            exit_price = take_line
            exit_reason = "take_profit"
            break
    if exit_price is None:
        # 到期未触发止损止盈：按最后一根收盘离场
        last = future.iloc[min(hold_max, len(future)) - 1]
        try:
            exit_price = float(last["close"])
        except (TypeError, ValueError, KeyError):
            return None
        exit_reason = "time_exit"

    # 卖出含滑点（向下）
    sell_price = exit_price * (1 - slip)
    # 成本：买卖双边佣金 + 卖出印花税
    cost_pct = params["commission_pct"] * 2 + params["stamp_tax_pct"]
    gross_ret = (sell_price - buy_price) / buy_price * 100
    net_ret = gross_ret - cost_pct
    return {
        "buy_price": round(buy_price, 3),
        "exit_price": round(sell_price, 3),
        "exit_reason": exit_reason,
        "hold_days": hold_days,
        "gross_ret_pct": round(gross_ret, 3),
        "net_ret_pct": round(net_ret, 3),
    }


def backtest_symbol(
    symbol: str,
    name: str,
    direction: str,
    config: dict[str, Any],
    days: int,
) -> list[dict[str, Any]]:
    """对单个标的回测，返回逐笔交易记录（已扣成本）。"""
    params = _cfg(config)
    strategy = config.get("strategy", {})
    is_etf = _is_etf(symbol)
    min_bars = int(params["min_history_bars"])

    # 多取一些历史，保证前 min_bars 根能算指标 + 后面有 T+1 空间
    total_bars = days + min_bars + int(params["hold_max_days"]) + 5
    try:
        hist = fetch_history(symbol, total_bars)
    except Exception as exc:
        print(f"[{symbol}] history fetch failed: {exc}")
        return []
    if hist is None or len(hist) < min_bars + 2:
        return []
    hist = hist.reset_index(drop=True)

    trades: list[dict[str, Any]] = []
    n = len(hist)
    # i 是"信号触发日"下标；需要 i 之前有 min_bars 根，i 之后至少 1 根做 T+1
    start = min_bars
    end = n - 2  # 保证 i+1 存在
    for i in range(start, end + 1):
        history_closed = hist.iloc[:i].reset_index(drop=True)  # 前 i 根已收盘
        row = hist.iloc[i]
        prev_close = float(hist.iloc[i - 1]["close"])
        snapshot = _row_to_snapshot(row, prev_close, symbol, name)
        if snapshot is None:
            continue
        try:
            signal = evaluate_signal(history_closed, snapshot, strategy, fraction=1.0)
        except Exception:
            continue
        if not signal or not signal.get("entry_triggered"):
            continue
        future = hist.iloc[i + 1:].reset_index(drop=True)
        trade = _simulate_trade(future, params, is_etf, trigger_close=float(row["close"]))
        if trade is None or trade.get("skipped"):
            continue
        trade.update({
            "symbol": symbol,
            "name": name,
            "direction": direction,
            "signal_date": str(row.get("date"))[:10],
            "strategy_label": signal.get("strategy_label") or signal.get("strategy"),
            "score": signal.get("score"),
        })
        trades.append(trade)
    return trades


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐笔交易聚合成量化指标。

    收益口径说明（关键，避免虚高）：这些交易在时间上大量重叠（同期常持有多只），
    绝不能按"一笔接一笔满仓复投"几何累乘 —— 那会算出 +76333% 这种荒谬数字。
    这里改用**固定仓位加法口径**：把每笔交易视为等额独立下注（如每笔投 1 个单位），
    汇总收益 = 各笔净收益之和（sum_return_pct）；回撤在"按信号日排序的加法累计曲线"
    上计算（单位：百分点）。这是策略胜率×赔率的诚实体现，不夸大复利。
    """
    n = len(trades)
    if n == 0:
        return {"trades": 0, "note": "无触发样本，无法评估"}
    rets = [t["net_ret_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # 盈亏比 = 平均盈利 / 平均亏损绝对值
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else float("inf")
    # 期望值（每笔平均净收益）= 胜率*均盈 + (1-胜率)*均亏
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # 加法口径累计与回撤：按信号日排序，逐笔累加净收益（百分点），求峰值回撤。
    ordered = sorted(trades, key=lambda t: str(t.get("signal_date") or ""))
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        cum += float(t["net_ret_pct"])
        peak = max(peak, cum)
        dd = cum - peak  # 距峰值的回撤（百分点）
        max_dd = min(max_dd, dd)
    sum_return = sum(rets)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    return {
        "trades": n,
        "win_rate": round(win_rate, 3),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "payoff_ratio": round(payoff, 3) if payoff != float("inf") else None,
        "expectancy_pct": round(expectancy, 3),
        "sum_return_pct": round(sum_return, 2),      # 固定仓位下各笔净收益之和（百分点）
        "max_drawdown_pp": round(max_dd, 2),         # 加法累计曲线最大回撤（百分点）
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / n, 1),
        "exit_reasons": reasons,
    }


def _group_metrics(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(t.get(key)), []).append(t)
    return {k: _metrics(v) for k, v in sorted(groups.items())}


def run_backtest(config: dict[str, Any], days: int, only_symbol: str | None = None) -> dict[str, Any]:
    # 汇总方向池 + 关注池的全部标的
    members: list[tuple[str, str, str]] = []  # (symbol, name, direction)
    for pool_key in ("direction_pool", "message_focus_pool"):
        for item in config.get(pool_key, []) or []:
            direction = item.get("direction", "未知")
            for m in item.get("members", []):
                members.append((m["symbol"], m.get("name") or m["symbol"], direction))
    # 去重（同一 symbol 只测一次，方向取首个）
    seen: set[str] = set()
    uniq: list[tuple[str, str, str]] = []
    for sym, nm, d in members:
        if only_symbol and sym != only_symbol:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        uniq.append((sym, nm, d))

    all_trades: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}
    for sym, nm, d in uniq:
        trades = backtest_symbol(sym, nm, d, config, days)
        per_symbol[f"{nm}({sym})"] = _metrics(trades)
        all_trades.extend(trades)

    return {
        "params": _cfg(config),
        "window_days": days,
        "symbols_tested": len(uniq),
        "overall": _metrics(all_trades),
        "by_strategy": _group_metrics(all_trades, "strategy_label"),
        "by_direction": _group_metrics(all_trades, "direction"),
        "by_symbol": per_symbol,
        "trades_sample": all_trades[-20:],  # 最近20笔样例，便于人工抽查
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A股策略回测引擎")
    parser.add_argument("--config", required=True)
    parser.add_argument("--days", type=int, default=250, help="回测最近多少个交易日")
    parser.add_argument("--symbol", default="", help="只测某只（6位代码）")
    parser.add_argument("--out", default="", help="结果写入的 JSON 文件路径（默认打印到 stdout）")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    result = run_backtest(config, args.days, args.symbol or None)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"backtest result written to {args.out}")
        print(json.dumps(result["overall"], ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
