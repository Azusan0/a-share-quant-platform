from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import pandas as pd


def _is_bad(value: Any) -> bool:
    """判断一个数值是否不可用（None / NaN / inf）。

    量能等指标缺失时必须显式识别，禁止让 NaN 穿透进 score 或排序。
    """
    if value is None:
        return True
    try:
        num = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(num) or math.isinf(num)


def _clean_score(value: Any) -> float:
    """把分数规整成可排序的实数：坏值一律视为最低分，永不排到前面。"""
    if _is_bad(value):
        return float("-inf")
    return float(value)


def _volume_ratio_same_unit(curr_volume_shares: float, avg_volume_shares: float) -> float | None:
    """同口径量比：当日成交量(股) vs 历史日均成交量(股)。

    任一侧不可用则返回 None（表示「无量能信号」），不返回 0 也不返回 NaN。
    """
    if _is_bad(curr_volume_shares) or _is_bad(avg_volume_shares) or avg_volume_shares <= 0:
        return None
    return curr_volume_shares / avg_volume_shares


def _expected_volume_fraction(now: datetime | None = None) -> float:
    """典型A股日内累计成交占比，用于把累计成交量换算成同期量比。"""
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute + now.second / 60
    anchors = [(570, .02), (600, .20), (630, .32), (690, .48),
               (780, .48), (810, .62), (870, .82), (900, 1.0)]
    if minute <= anchors[0][0]:
        return anchors[0][1]
    if minute >= anchors[-1][0]:
        return 1.0
    for (left_m, left_v), (right_m, right_v) in zip(anchors, anchors[1:]):
        if left_m <= minute <= right_m:
            if right_v == left_v:
                return left_v
            return left_v + (right_v - left_v) * (minute - left_m) / (right_m - left_m)
    return 1.0


def _same_time_volume_ratio(
    curr_volume_shares: float,
    avg_volume_shares: float,
    now: datetime | None = None,
    fraction: float | None = None,
) -> float | None:
    raw = _volume_ratio_same_unit(curr_volume_shares, avg_volume_shares)
    fraction = fraction if fraction is not None else _expected_volume_fraction(now)
    return raw / fraction if raw is not None and fraction > 0 else None


def _overheat_reasons(rsi: float, ma_bias_pct: float, close_to_high_ratio: float, params: dict[str, Any]) -> list[str]:
    """识别「短线过热」——追这类信号最容易隔天回落。返回命中的过热理由列表。

    阈值可在 config.strategy 里覆盖；默认与日报 _classify_setup 的口径一致。
    """
    reasons: list[str] = []
    rsi_hot = float(params.get("overheat_rsi", 74))
    bias_hot = float(params.get("overheat_ma_bias_pct", 4.5))
    high_hot = float(params.get("overheat_close_to_high_ratio", 0.9))
    if not _is_bad(rsi) and rsi >= rsi_hot:
        reasons.append(f"RSI偏高{rsi:.1f}")
    if not _is_bad(ma_bias_pct) and ma_bias_pct >= bias_hot:
        reasons.append(f"离短均线偏远{ma_bias_pct:.1f}%")
    if not _is_bad(close_to_high_ratio) and close_to_high_ratio >= high_hot:
        reasons.append(f"贴近日内高点{close_to_high_ratio:.2f}")
    return reasons


def _is_limit_up_lock(high: float, low: float, change_pct: float, *, is_etf: bool) -> bool:
    """一字/准一字涨停锁死判定。

    此时 high==low，close_to_high_ratio 会算成 0 而被入场门槛误杀，
    但这恰恰是最强走势，需单独识别。个股涨停约 9.8%+、ETF/科创/创业板放宽。
    """
    if _is_bad(high) or _is_bad(low) or _is_bad(change_pct):
        return False
    threshold = 4.5 if is_etf else 9.5
    return abs(high - low) <= 1e-9 and change_pct >= threshold


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = pd.Series(series, dtype="float64")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return pd.Series(rsi).fillna(0)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def evaluate_oversold_rebound(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    params: dict[str, Any],
    now: datetime | None = None,
    fraction: float | None = None,
) -> dict[str, Any] | None:
    rsi_period = int(params["rsi_period"])
    ma_period = int(params["ma_period"])
    volume_ma_period = int(params.get("volume_ma_period", 5))
    min_volume_ratio = float(params.get("min_volume_ratio", 1.0))
    min_intraday_rebound_pct = float(params.get("min_intraday_rebound_pct", 0.3))
    min_close_to_high_ratio = float(params.get("min_close_to_high_ratio", 0.6))
    min_body_to_range_ratio = float(params.get("min_body_to_range_ratio", 0.2))

    min_required_bars = max(rsi_period + 2, ma_period, volume_ma_period)
    if history.empty or len(history) < min_required_bars:
        return None

    df = history.copy()
    close_col = pd.Series(pd.to_numeric(df["close"], errors="coerce"), dtype="float64")
    open_col = pd.Series(pd.to_numeric(df["open"], errors="coerce"), dtype="float64")
    volume_col = pd.Series(pd.to_numeric(df.get("volume"), errors="coerce"), dtype="float64")

    df["rsi"] = compute_rsi(close_col, rsi_period)
    df["ma"] = close_col.rolling(ma_period).mean()
    df["avg_volume"] = volume_col.rolling(volume_ma_period).mean()

    prev_rsi = float(df["rsi"].iloc[-2])
    curr_rsi = float(df["rsi"].iloc[-1])
    curr_ma = float(df["ma"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])

    curr_price = float(snapshot["price"])
    curr_open = float(snapshot["open"])
    curr_high = float(snapshot["high"])
    curr_low = float(snapshot["low"])
    change_pct = float(snapshot["change_pct"])
    curr_amount = float(snapshot["amount"])
    # 当日成交量(股) = 成交额(元)/价格，与历史 volume(已统一为股)同口径。
    curr_volume_shares = curr_amount / curr_price if curr_price > 0 else float("nan")

    oversold = float(params["rsi_oversold"])
    min_change_pct = float(params["min_change_pct"])

    crossed_up = prev_rsi < oversold and curr_rsi >= oversold
    above_ma = curr_price >= curr_ma
    change_ok = change_pct >= min_change_pct

    # 同口径量比；缺失时不作为放量证据（该策略要求放量确认，缺失则视为不满足）。
    volume_ratio = _same_time_volume_ratio(curr_volume_shares, avg_volume, now=now, fraction=fraction)
    volume_available = volume_ratio is not None
    volume_ok = volume_available and volume_ratio >= min_volume_ratio

    intraday_rebound_pct = _safe_ratio(curr_price - curr_low, curr_low) * 100 if curr_low > 0 else 0.0
    high_low_range = curr_high - curr_low
    close_to_high_ratio = _safe_ratio(curr_price - curr_low, high_low_range) if high_low_range > 0 else 0.0
    body_to_range_ratio = _safe_ratio(abs(curr_price - curr_open), high_low_range) if high_low_range > 0 else 0.0

    rebound_ok = intraday_rebound_pct >= min_intraday_rebound_pct
    location_ok = close_to_high_ratio >= min_close_to_high_ratio
    body_ok = body_to_range_ratio >= min_body_to_range_ratio

    if not (crossed_up and above_ma and change_ok and volume_ok and rebound_ok and location_ok and body_ok):
        return None

    return {
        "signal_type": "entry",
        "strategy": "oversold_rebound",
        "price": curr_price,
        "change_pct": change_pct,
        "rsi": round(curr_rsi, 2),
        "ma": round(curr_ma, 3),
        "volume_ratio": round(volume_ratio, 2) if volume_available else None,
        "volume_available": volume_available,
        "intraday_rebound_pct": round(intraday_rebound_pct, 2),
        "close_to_high_ratio": round(close_to_high_ratio, 2),
        "body_to_range_ratio": round(body_to_range_ratio, 2),
        "message": f"{snapshot['name']} 触发增强版超跌反弹观察信号",
    }


def score_direction_rotation(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    params: dict[str, Any],
    now: datetime | None = None,
    fraction: float | None = None,
) -> dict[str, Any] | None:
    ma_period = int(params.get("ma_period", 5))
    momentum_period = int(params.get("momentum_period", 10))
    volume_ma_period = int(params.get("volume_ma_period", 5))
    amount_ma_period = int(params.get("amount_ma_period", 5))
    min_score = float(params.get("min_score", 60))
    entry_min_score = float(params.get("entry_min_score", min_score + 5))
    entry_min_change_pct = float(params.get("entry_min_change_pct", 0.5))
    entry_min_close_to_high_ratio = float(params.get("entry_min_close_to_high_ratio", 0.65))
    entry_min_volume_ratio = float(params.get("entry_min_volume_ratio", 1.0))
    entry_min_amount_ratio = float(params.get("entry_min_amount_ratio", 1.0))
    entry_min_ma_bias_pct = float(params.get("entry_min_ma_bias_pct", 0.5))
    min_body_to_range_ratio = float(params.get("min_body_to_range_ratio", 0.2))

    min_required_bars = max(ma_period, momentum_period + 1, volume_ma_period, amount_ma_period)
    if history.empty or len(history) < min_required_bars:
        return None

    symbol = str(snapshot.get("symbol", ""))
    is_etf = symbol.startswith(("15", "51", "56", "58", "52"))

    df = history.copy()
    close_col = pd.Series(pd.to_numeric(df["close"], errors="coerce"), dtype="float64")
    volume_col = pd.Series(pd.to_numeric(df.get("volume"), errors="coerce"), dtype="float64")
    amount_col = pd.Series(pd.to_numeric(df.get("amount", pd.Series(index=df.index, dtype="float64")), errors="coerce"), dtype="float64")

    df["rsi"] = compute_rsi(close_col, int(params.get("rsi_period", 14)))
    df["ma"] = close_col.rolling(ma_period).mean()
    df["avg_volume"] = volume_col.rolling(volume_ma_period).mean()
    df["avg_amount"] = amount_col.rolling(amount_ma_period).mean()

    curr_price = float(snapshot["price"])
    curr_open = float(snapshot["open"])
    curr_high = float(snapshot["high"])
    curr_low = float(snapshot["low"])
    change_pct = float(snapshot["change_pct"])
    curr_amount = float(snapshot["amount"])
    # 当日成交量(股)：由成交额(元)/价格换算，与历史 volume(已统一为股)同口径。
    curr_volume_shares = curr_amount / curr_price if curr_price > 0 else float("nan")

    curr_ma = float(df["ma"].iloc[-1])
    curr_rsi = float(df["rsi"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])
    avg_amount = float(df["avg_amount"].iloc[-1])
    prev_close = float(close_col.iloc[-momentum_period - 1])

    ma_bias_pct = _safe_ratio(curr_price - curr_ma, curr_ma) * 100 if curr_ma > 0 else 0.0
    momentum_pct = _safe_ratio(curr_price - prev_close, prev_close) * 100 if prev_close > 0 else 0.0
    # 量比：同口径(股 vs 股)，任一侧缺失则 None，不参与加分、也不误判为放量。
    volume_ratio = _same_time_volume_ratio(curr_volume_shares, avg_volume, now=now, fraction=fraction)
    volume_available = volume_ratio is not None
    volume_fraction = fraction if fraction is not None else _expected_volume_fraction(now)
    amount_ratio = _safe_ratio(curr_amount, avg_amount) / volume_fraction if avg_amount > 0 and math.isfinite(avg_amount) and volume_fraction > 0 else None
    high_low_range = curr_high - curr_low
    limit_up = _is_limit_up_lock(curr_high, curr_low, change_pct, is_etf=is_etf)
    if limit_up:
        # 一字/准一字涨停：high==low 会把位置/实体算成 0，这里视为最强走势直接给满。
        close_to_high_ratio = 1.0
        body_to_range_ratio = 1.0
    else:
        close_to_high_ratio = _safe_ratio(curr_price - curr_low, high_low_range) if high_low_range > 0 else 0.0
        body_to_range_ratio = _safe_ratio(abs(curr_price - curr_open), high_low_range) if high_low_range > 0 else 0.0

    score = 0.0
    score += min(max(change_pct, 0.0) * 10, 20)
    score += min(max(ma_bias_pct, 0.0) * 8, 20)
    score += min(max(momentum_pct, 0.0) * 4, 15)
    score += min(max(curr_rsi - 50, 0.0), 15)
    # 量能项：只有拿到可信量比才加分；缺失时不加分也不扣分（避免 NaN 与假放量）。
    if volume_available:
        score += min(max(volume_ratio - 1.0, 0.0) * 20, 25)
    score += min(close_to_high_ratio * 5, 5)

    if body_to_range_ratio < min_body_to_range_ratio:
        score -= 10

    # 过热判定（供触发端做熔断，不在这里直接改分）。
    hot_reasons = _overheat_reasons(curr_rsi, ma_bias_pct, close_to_high_ratio, params)

    passed = score >= min_score
    # 正式入场时，缺失量能必须降级为观察，不能把未知视为通过。
    volume_gate = volume_available and volume_ratio >= entry_min_volume_ratio
    amount_gate = amount_ratio is None or amount_ratio >= entry_min_amount_ratio
    entry_triggered = (
        score >= entry_min_score
        and change_pct >= entry_min_change_pct
        and (close_to_high_ratio >= entry_min_close_to_high_ratio or limit_up)
        and ma_bias_pct >= entry_min_ma_bias_pct
        and (body_to_range_ratio >= min_body_to_range_ratio or limit_up)
        and volume_gate
        and amount_gate
    )

    entry_reasons: list[str] = []
    if limit_up:
        entry_reasons.append("涨停锁死（最强走势）")
    if score >= entry_min_score:
        entry_reasons.append(f"方向评分{score:.1f}")
    if change_pct >= entry_min_change_pct:
        entry_reasons.append(f"当日涨幅{change_pct:.2f}%")
    if ma_bias_pct >= entry_min_ma_bias_pct:
        entry_reasons.append(f"站上短均线{ma_bias_pct:.2f}%")
    if close_to_high_ratio >= entry_min_close_to_high_ratio and not limit_up:
        entry_reasons.append(f"收在日内高位附近{close_to_high_ratio:.2f}")
    if volume_available and volume_ratio >= entry_min_volume_ratio:
        entry_reasons.append(f"量能放大 {volume_ratio:.2f}倍")
    elif not volume_available:
        entry_reasons.append("量能数据缺失（未确认放量）")

    return {
        "signal_type": "entry",
        "strategy": "direction_rotation",
        "strategy_label": "右侧突破",
        "passed": passed,
        "entry_triggered": entry_triggered,
        "entry_reasons": entry_reasons,
        "score": round(score, 2),
        "price": curr_price,
        "change_pct": change_pct,
        "rsi": round(curr_rsi, 2),
        "ma_bias_pct": round(ma_bias_pct, 2),
        "momentum_pct": round(momentum_pct, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_available else None,
        "volume_available": volume_available,
        "amount_ratio": round(amount_ratio, 2) if amount_ratio is not None else None,
        "amount_available": amount_ratio is not None,
        "limit_up": limit_up,
        "overheated": bool(hot_reasons),
        "overheat_reasons": hot_reasons,
        "close_to_high_ratio": round(close_to_high_ratio, 2),
        "body_to_range_ratio": round(body_to_range_ratio, 2),
    }


def evaluate_pullback_buy(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    params: dict[str, Any],
    now: datetime | None = None,
    fraction: float | None = None,
) -> dict[str, Any] | None:
    ma_period = int(params.get("ma_period", 5))
    rsi_period = int(params.get("rsi_period", 14))
    momentum_period = int(params.get("momentum_period", 10))
    volume_ma_period = int(params.get("volume_ma_period", 5))
    min_required_bars = max(ma_period, rsi_period + 1, momentum_period + 1, volume_ma_period)
    if history.empty or len(history) < min_required_bars:
        return None

    df = history.copy()
    close_col = pd.Series(pd.to_numeric(df["close"], errors="coerce"), dtype="float64")
    volume_col = pd.Series(pd.to_numeric(df.get("volume"), errors="coerce"), dtype="float64")
    df["rsi"] = compute_rsi(close_col, rsi_period)
    df["ma"] = close_col.rolling(ma_period).mean()
    df["avg_volume"] = volume_col.rolling(volume_ma_period).mean()

    curr_price = float(snapshot["price"])
    curr_open = float(snapshot["open"])
    curr_high = float(snapshot["high"])
    curr_low = float(snapshot["low"])
    change_pct = float(snapshot["change_pct"])
    curr_amount = float(snapshot["amount"])
    curr_volume_shares = curr_amount / curr_price if curr_price > 0 else float("nan")

    curr_ma = float(df["ma"].iloc[-1])
    curr_rsi = float(df["rsi"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])
    prev_close = float(close_col.iloc[-momentum_period - 1])

    ma_bias_pct = _safe_ratio(curr_price - curr_ma, curr_ma) * 100 if curr_ma > 0 else 0.0
    momentum_pct = _safe_ratio(curr_price - prev_close, prev_close) * 100 if prev_close > 0 else 0.0
    # 同口径量比；缺失时不作为「缩量」证据（回踩低吸要求缩量，缺失则该条件视为不满足）。
    volume_ratio = _same_time_volume_ratio(curr_volume_shares, avg_volume, now=now, fraction=fraction)
    volume_available = volume_ratio is not None
    high_low_range = curr_high - curr_low
    close_to_high_ratio = _safe_ratio(curr_price - curr_low, high_low_range) if high_low_range > 0 else 0.0
    body_to_range_ratio = _safe_ratio(abs(curr_price - curr_open), high_low_range) if high_low_range > 0 else 0.0

    volume_shrink_ok = volume_available and volume_ratio <= float(params.get("pullback_max_volume_ratio", 1.2))
    triggered = (
        momentum_pct >= float(params.get("pullback_min_momentum_pct", 8.0))
        and -float(params.get("pullback_max_ma_gap_pct", 1.5)) <= ma_bias_pct <= float(params.get("pullback_max_ma_bias_pct", 2.5))
        and 48 <= curr_rsi <= float(params.get("pullback_max_rsi", 68))
        and change_pct >= float(params.get("pullback_min_change_pct", -1.0))
        and close_to_high_ratio >= float(params.get("pullback_min_close_to_high_ratio", 0.45))
        and body_to_range_ratio >= float(params.get("pullback_min_body_to_range_ratio", 0.15))
        and volume_shrink_ok
    )
    if not triggered:
        return None

    hot_reasons = _overheat_reasons(curr_rsi, ma_bias_pct, close_to_high_ratio, params)
    entry_reasons = [
        f"前期动能{momentum_pct:.2f}%",
        f"回踩短均线附近{ma_bias_pct:.2f}%",
        f"RSI处于不过热区{curr_rsi:.2f}",
        f"收盘位置修复{close_to_high_ratio:.2f}",
    ]
    return {
        "signal_type": "entry",
        "strategy": "pullback_buy",
        "strategy_label": "回踩低吸",
        "passed": True,
        "entry_triggered": True,
        "entry_reasons": entry_reasons,
        "score": round(62 + min(momentum_pct, 20) * 0.6 + max(0, close_to_high_ratio - 0.45) * 20, 2),
        "price": curr_price,
        "change_pct": change_pct,
        "rsi": round(curr_rsi, 2),
        "ma_bias_pct": round(ma_bias_pct, 2),
        "momentum_pct": round(momentum_pct, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_available else None,
        "volume_available": volume_available,
        "limit_up": False,
        "overheated": bool(hot_reasons),
        "overheat_reasons": hot_reasons,
        "close_to_high_ratio": round(close_to_high_ratio, 2),
        "body_to_range_ratio": round(body_to_range_ratio, 2),
    }


def evaluate_rebound_wrap(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    params: dict[str, Any],
    now: datetime | None = None,
    fraction: float | None = None,
) -> dict[str, Any] | None:
    ma_period = int(params.get("ma_period", 5))
    rsi_period = int(params.get("rsi_period", 14))
    volume_ma_period = int(params.get("volume_ma_period", 5))
    min_required_bars = max(ma_period + 1, rsi_period + 1, volume_ma_period)
    if history.empty or len(history) < min_required_bars:
        return None

    df = history.copy()
    close_col = pd.Series(pd.to_numeric(df["close"], errors="coerce"), dtype="float64")
    open_col = pd.Series(pd.to_numeric(df["open"], errors="coerce"), dtype="float64")
    volume_col = pd.Series(pd.to_numeric(df.get("volume", 0), errors="coerce"), dtype="float64")
    df["rsi"] = compute_rsi(close_col, rsi_period)
    df["ma"] = close_col.rolling(ma_period).mean()
    df["avg_volume"] = volume_col.rolling(volume_ma_period).mean()

    prev_close = float(close_col.iloc[-2])
    prev_open = float(open_col.iloc[-2])
    prev_body_pct = _safe_ratio(prev_close - prev_open, prev_open) * 100 if prev_open > 0 else 0.0
    prev_change_pct = _safe_ratio(prev_close - float(close_col.iloc[-3]), float(close_col.iloc[-3])) * 100 if len(close_col) >= 3 and float(close_col.iloc[-3]) > 0 else 0.0
    curr_price = float(snapshot["price"])
    curr_open = float(snapshot["open"])
    curr_high = float(snapshot["high"])
    curr_low = float(snapshot["low"])
    change_pct = float(snapshot["change_pct"])
    curr_amount = float(snapshot["amount"])
    curr_volume_shares = curr_amount / curr_price if curr_price > 0 else float("nan")

    curr_ma = float(df["ma"].iloc[-1])
    curr_rsi = float(df["rsi"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])
    ma_bias_pct = _safe_ratio(curr_price - curr_ma, curr_ma) * 100 if curr_ma > 0 else 0.0
    # 同口径量比；反包要求放量确认，量能缺失时该条件视为不满足（宁可漏，不假确认）。
    volume_ratio = _same_time_volume_ratio(curr_volume_shares, avg_volume, now=now, fraction=fraction)
    volume_available = volume_ratio is not None
    high_low_range = curr_high - curr_low
    close_to_high_ratio = _safe_ratio(curr_price - curr_low, high_low_range) if high_low_range > 0 else 0.0
    body_to_range_ratio = _safe_ratio(abs(curr_price - curr_open), high_low_range) if high_low_range > 0 else 0.0

    volume_expand_ok = volume_available and volume_ratio >= float(params.get("rebound_min_volume_ratio", 1.0))
    triggered = (
        prev_change_pct <= -float(params.get("rebound_prev_drop_pct", 1.5))
        and change_pct >= float(params.get("rebound_min_change_pct", 1.0))
        and curr_price >= prev_close
        and close_to_high_ratio >= float(params.get("rebound_min_close_to_high_ratio", 0.6))
        and body_to_range_ratio >= float(params.get("rebound_min_body_to_range_ratio", 0.25))
        and volume_expand_ok
        and ma_bias_pct >= -float(params.get("rebound_max_ma_gap_pct", 1.0))
    )
    if not triggered:
        return None

    hot_reasons = _overheat_reasons(curr_rsi, ma_bias_pct, close_to_high_ratio, params)
    entry_reasons = [
        f"前一日回调{prev_change_pct:.2f}%",
        f"今日反包涨幅{change_pct:.2f}%",
        "收回前一日收盘附近",
        f"量能修复{volume_ratio:.2f}倍",
    ]
    return {
        "signal_type": "entry",
        "strategy": "rebound_wrap",
        "strategy_label": "回踩反包",
        "passed": True,
        "entry_triggered": True,
        "entry_reasons": entry_reasons,
        "score": round(65 + min(change_pct, 8) * 2 + min(volume_ratio, 2.5) * 4, 2),
        "price": curr_price,
        "change_pct": change_pct,
        "rsi": round(curr_rsi, 2),
        "ma_bias_pct": round(ma_bias_pct, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_available else None,
        "volume_available": volume_available,
        "limit_up": False,
        "overheated": bool(hot_reasons),
        "overheat_reasons": hot_reasons,
        "close_to_high_ratio": round(close_to_high_ratio, 2),
        "body_to_range_ratio": round(body_to_range_ratio, 2),
    }


def evaluate_exit_signal(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    params: dict[str, Any],
    position: dict[str, Any],
) -> dict[str, Any] | None:
    ma_period = int(params.get("exit_ma_period", 5))
    rsi_period = int(params.get("rsi_period", 14))
    exit_rsi_threshold = float(params.get("exit_rsi_threshold", 75))
    stop_loss_pct = float(params.get("stop_loss_pct", -5.0))
    take_profit_pct = float(params.get("take_profit_pct", 15.0))
    max_drawdown_from_high_pct = float(params.get("max_drawdown_from_high_pct", -4.0))
    min_required_bars = max(ma_period, rsi_period + 1)
    if history.empty or len(history) < min_required_bars:
        return None

    buy_price = float(position.get("buy_price", 0))
    if buy_price <= 0:
        return None

    df = history.copy()
    close_col = pd.Series(pd.to_numeric(df["close"], errors="coerce"), dtype="float64")
    df["rsi"] = compute_rsi(close_col, rsi_period)
    df["ma"] = close_col.rolling(ma_period).mean()

    curr_price = float(snapshot["price"])
    curr_rsi = float(df["rsi"].iloc[-1])
    curr_ma = float(df["ma"].iloc[-1])
    curr_open = float(snapshot["open"])
    curr_high = float(snapshot["high"])
    curr_low = float(snapshot["low"])
    pnl_pct = _safe_ratio(curr_price - buy_price, buy_price) * 100

    highest_price = max(float(position.get("highest_price", buy_price)), curr_high, curr_price)
    drawdown_from_high_pct = _safe_ratio(curr_price - highest_price, highest_price) * 100 if highest_price > 0 else 0.0
    high_low_range = curr_high - curr_low
    close_to_low_ratio = _safe_ratio(curr_price - curr_low, high_low_range) if high_low_range > 0 else 1.0
    body_to_range_ratio = _safe_ratio(abs(curr_price - curr_open), high_low_range) if high_low_range > 0 else 0.0

    exit_reasons: list[str] = []
    if pnl_pct <= stop_loss_pct:
        exit_reasons.append(f"跌破止损线{pnl_pct:.2f}%")
    if pnl_pct >= take_profit_pct and drawdown_from_high_pct <= max_drawdown_from_high_pct:
        exit_reasons.append(f"高位回撤{drawdown_from_high_pct:.2f}%")
    if curr_price < curr_ma and close_to_low_ratio <= 0.35:
        exit_reasons.append("跌回短均线下且收盘偏弱")
    if curr_rsi >= exit_rsi_threshold and body_to_range_ratio < 0.2:
        exit_reasons.append(f"高位钝化转弱 RSI{curr_rsi:.1f}")

    if not exit_reasons:
        return None

    return {
        "signal_type": "exit",
        "strategy": "position_exit",
        "symbol": position.get("symbol"),
        "name": position.get("name") or snapshot.get("name") or position.get("symbol"),
        "buy_price": round(buy_price, 3),
        "current_price": round(curr_price, 3),
        "pnl_pct": round(pnl_pct, 2),
        "highest_price": round(highest_price, 3),
        "drawdown_from_high_pct": round(drawdown_from_high_pct, 2),
        "rsi": round(curr_rsi, 2),
        "ma": round(curr_ma, 3),
        "exit_reasons": exit_reasons,
        "message": f"{position.get('name') or position.get('symbol')} 触发离场观察信号：{'、'.join(exit_reasons)}",
    }


def evaluate_signal(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    strategy: dict[str, Any],
    now: datetime | None = None,
    fraction: float | None = None,
) -> dict[str, Any] | None:
    name = strategy.get("name")
    if name == "oversold_rebound":
        return evaluate_oversold_rebound(history, snapshot, strategy, now=now, fraction=fraction)
    if name == "direction_rotation":
        breakout_signal = score_direction_rotation(history, snapshot, strategy, now=now, fraction=fraction)
        pullback_signal = evaluate_pullback_buy(history, snapshot, strategy, now=now, fraction=fraction)
        rebound_signal = evaluate_rebound_wrap(history, snapshot, strategy, now=now, fraction=fraction)
        candidates = [item for item in [breakout_signal, pullback_signal, rebound_signal] if item]
        # 分数为 NaN/None 的候选一律视为最低分，绝不排到最前（否则可能把指标缺失的标的当成最佳）。
        candidates = [item for item in candidates if _clean_score(item.get("score")) != float("-inf")]
        if not candidates:
            return None
        candidates.sort(key=lambda item: _clean_score(item.get("score")), reverse=True)
        return candidates[0]
    raise ValueError(f"unsupported strategy: {name}")
