"""大盘环境择时（#3）。

目的：所有个股/方向信号在系统性下跌里都会失效。开盘出手前先看大盘 regime，
环境差（risk_off）时全局降级——把入场提醒降级为「大盘偏弱观望」，避免在普跌里追高。

设计原则：任何数据抓取失败一律回退到 neutral（中性，不额外压制），
绝不因指数抓取失败而误判 risk_off 把所有信号砍掉，也绝不抛异常拖垮监控主流程。
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd

# 用上证指数作为大盘基准。腾讯快照代码 / akshare 历史代码。
_INDEX_SYMBOL = "sh000001"
_REGIME_CACHE: dict[str, Any] | None = None


def _safe_ma(close: pd.Series, period: int) -> float | None:
    if close is None or len(close) < period:
        return None
    val = float(close.tail(period).mean())
    return val if val == val else None  # NaN 检查


def _fetch_index_history(bars: int = 90) -> pd.DataFrame | None:
    """取上证指数日线。多源回退，全失败返回 None。"""
    try:
        import akshare as ak

        df = ak.stock_zh_index_daily(symbol=_INDEX_SYMBOL)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "date", "close": "close"})
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.tail(bars).reset_index(drop=True)
    except Exception:
        return None


def _fetch_index_snapshot() -> dict[str, Any] | None:
    """取上证指数当日快照（涨跌幅）。失败返回 None。"""
    try:
        import requests

        r = requests.get(f"https://qt.gtimg.cn/q={_INDEX_SYMBOL}", timeout=10)
        r.raise_for_status()
        body = r.content.decode("gbk", errors="ignore")
        fields = body.split('="', 1)[1].rsplit('"', 1)[0].split("~")
        price = float(fields[3])
        prev_close = float(fields[4])
        change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
        return {"price": price, "change_pct": round(change_pct, 2)}
    except Exception:
        return None


def evaluate_market_regime(
    config: dict[str, Any],
    *,
    history_fetcher: Callable[[], pd.DataFrame | None] = _fetch_index_history,
    snapshot_fetcher: Callable[[], dict[str, Any] | None] = _fetch_index_snapshot,
    use_cache: bool = True,
) -> dict[str, Any]:
    """判定大盘环境。返回 {level, score, reasons, detail}。

    level ∈ {risk_on, neutral, risk_off}。抓取失败 → neutral。
    """
    global _REGIME_CACHE
    if use_cache and _REGIME_CACHE is not None:
        return _REGIME_CACHE

    cfg = config.get("market_regime", {}) if isinstance(config, dict) else {}
    ma_short = int(cfg.get("ma_short", 20))
    ma_long = int(cfg.get("ma_long", 60))

    hist = history_fetcher()
    snap = snapshot_fetcher()

    if hist is None or hist.empty:
        result = {
            "level": "neutral",
            "score": 0,
            "reasons": ["指数数据不可用，按中性处理（不额外压制）"],
            "detail": {"source": "unavailable"},
        }
        if use_cache:
            _REGIME_CACHE = result
        return result

    close = pd.Series(pd.to_numeric(hist["close"], errors="coerce"), dtype="float64").dropna()
    curr = float(close.iloc[-1])
    ma_s = _safe_ma(close, ma_short)
    ma_l = _safe_ma(close, ma_long)
    change_pct = float(snap["change_pct"]) if snap else None

    reasons: list[str] = []
    score = 0
    above_short = ma_s is not None and curr >= ma_s
    above_long = ma_l is not None and curr >= ma_l

    if above_short:
        score += 1
        reasons.append(f"指数站上{ma_short}日均线")
    else:
        score -= 1
        reasons.append(f"指数跌破{ma_short}日均线")
    if above_long:
        score += 1
        reasons.append(f"指数站上{ma_long}日均线")
    else:
        score -= 1
        reasons.append(f"指数跌破{ma_long}日均线")

    if change_pct is not None:
        if change_pct <= -1.5:
            score -= 1
            reasons.append(f"当日大盘重挫{change_pct:.2f}%")
        elif change_pct >= 1.0:
            score += 1
            reasons.append(f"当日大盘走强{change_pct:.2f}%")

    if score >= 2:
        level = "risk_on"
    elif score <= -1:
        level = "risk_off"
    else:
        level = "neutral"

    result = {
        "level": level,
        "score": score,
        "reasons": reasons,
        "detail": {
            "index": _INDEX_SYMBOL,
            "close": round(curr, 2),
            "ma_short": round(ma_s, 2) if ma_s is not None else None,
            "ma_long": round(ma_l, 2) if ma_l is not None else None,
            "change_pct": change_pct,
            "source": "akshare+tencent",
        },
    }
    if use_cache:
        _REGIME_CACHE = result
    return result
