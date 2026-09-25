from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import akshare as ak
import numpy as np
import pandas as pd
import requests
import sys
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

# 统一口径约定（关键）：
#   volume -> 股（不是「手」）
#   amount -> 元
# 各数据源单位不一致，且部分源缺列，若不统一会导致 volume_ratio/amount_ratio 失真甚至 NaN。
# 缺列一律显式置 NaN（不要用 0 冒充），交由策略层判定为「无量能信号」。
CANONICAL_COLUMNS = ["date", "open", "close", "high", "low", "volume", "amount"]
LOTS_TO_SHARES = 100  # 1 手 = 100 股


def _canonicalize(df: pd.DataFrame, *, volume_in_lots: bool, has_amount: bool) -> pd.DataFrame:
    """把任意来源的历史日线统一成 CANONICAL_COLUMNS 口径。

    volume_in_lots: 源里的成交量是否以「手」为单位（akshare/腾讯/新浪日线均为手）。
    has_amount: 源是否提供真实成交额（元）。腾讯 tx、新浪 ETF 接口不提供。
    """
    out = df.copy()
    for col in ("open", "close", "high", "low"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "volume" in out.columns:
        vol = pd.to_numeric(out["volume"], errors="coerce")
        out["volume"] = vol * LOTS_TO_SHARES if volume_in_lots else vol
    else:
        out["volume"] = np.nan

    if has_amount and "amount" in out.columns:
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce")
    else:
        # 缺真实成交额：显式置 NaN，禁止用成交量数值冒充成交额。
        out["amount"] = np.nan

    out["date"] = pd.to_datetime(out["date"])
    for col in CANONICAL_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    return out[CANONICAL_COLUMNS]


def _rename_cn(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "change_pct",
    }
    return df.rename(columns=rename_map)


def _market_prefix(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def _is_etf(symbol: str) -> bool:
    return symbol.startswith(("15", "51", "56", "58", "52"))


def _to_float(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    return float(value)


def _drop_unclosed_today(df: pd.DataFrame) -> pd.DataFrame:
    """剔除「当日未收盘」那根日线。

    盘中调用日线接口时，部分源会把当天这根半截 K 线也返回；下游指标（ma/rsi/prev_close）
    只应基于已收盘日线，当日行情由 live snapshot 提供，否则同一天被算两次且口径不一致，
    会造成 ma_bias_pct/momentum_pct 严重偏差。
    """
    if df.empty or "date" not in df.columns:
        return df
    today = pd.Timestamp.now().normalize()
    return df[df["date"] < today].reset_index(drop=True)


def fetch_history(symbol: str, history_bars: int = 60) -> pd.DataFrame:
    fetchers = [_fetch_history_akshare]
    if _is_etf(symbol):
        fetchers.extend([_fetch_history_etf_sina, _fetch_history_tx])
    else:
        fetchers.extend([_fetch_history_tx])

    last_exc: Exception | None = None
    for fetcher in fetchers:
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                df = fetcher(symbol)
            if df is None or df.empty:
                raise RuntimeError("empty history frame")
            df = _drop_unclosed_today(df)
            return df.tail(history_bars).reset_index(drop=True)
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError(f"no history fetcher available for {symbol}")


def fetch_histories(symbols: list[str], history_bars: int = 60, max_workers: int = 4) -> dict[str, pd.DataFrame]:
    """Fetch history for multiple symbols concurrently.

    Returns dict[symbol -> DataFrame]. On failure for an individual symbol,
    prints an error to stderr (same behavior as monitor.py's except block)
    and omits it from the result dict.
    """
    result: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        fut_map = {
            executor.submit(fetch_history, symbol, history_bars): symbol
            for symbol in symbols
        }
        for future in as_completed(fut_map):
            symbol = fut_map[future]
            try:
                result[symbol] = future.result()
            except Exception as exc:
                print(f"history fetch failed for {symbol}: {exc}", file=sys.stderr)
                continue
    return result


def _fetch_history_akshare(symbol: str) -> pd.DataFrame:
    # 东财日线单位并不完全一致：A 股成交量为「手」，ETF 成交量已是「份」。
    # ETF 若再次乘 100，会让历史均量膨胀百倍，导致实盘量比长期只有 0.01 左右。
    df = _rename_cn(ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq"))
    return _canonicalize(df, volume_in_lots=not _is_etf(symbol), has_amount=True)


def _fetch_history_tx(symbol: str) -> pd.DataFrame:
    # 腾讯日线：个股成交量为「手」；ETF 回退数据与快照核对后已是「份」。
    tx_symbol = f"{_market_prefix(symbol)}{symbol}"
    with redirect_stdout(StringIO()):
        df = ak.stock_zh_a_hist_tx(symbol=tx_symbol, adjust="qfq")
    df = df.rename(columns={"amount": "volume"})
    return _canonicalize(df, volume_in_lots=not _is_etf(symbol), has_amount=False)


def _fetch_history_etf_sina(symbol: str) -> pd.DataFrame:
    # 新浪 ETF 日线：volume 已是「份」，不能再乘 100。
    sina_symbol = f"{_market_prefix(symbol)}{symbol}"
    df = ak.fund_etf_hist_sina(symbol=sina_symbol)
    return _canonicalize(df, volume_in_lots=False, has_amount=False)


def fetch_snapshot(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        return _fetch_snapshot_tencent(symbols)
    except Exception:
        return _fetch_snapshot_akshare(symbols)


def _validate_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    price = float(row.get("price") or 0)
    open_price = float(row.get("open") or 0)
    high = float(row.get("high") or 0)
    low = float(row.get("low") or 0)
    amount = float(row.get("amount") or 0)
    if min(price, high, low) <= 0:
        raise ValueError(f"invalid snapshot price fields for {row.get('symbol')}")
    if open_price <= 0:
        open_price = price
        row["open"] = price
    if high < max(price, open_price, low) or low > min(price, open_price, high):
        raise ValueError(f"inconsistent high/low fields for {row.get('symbol')}")
    if amount < 0:
        raise ValueError(f"negative amount for {row.get('symbol')}")
    return row


def _fetch_snapshot_akshare(symbols: list[str]) -> dict[str, dict[str, Any]]:
    df = ak.stock_zh_a_spot_em()
    subset = df[df["代码"].isin(symbols)].copy()
    result: dict[str, dict[str, Any]] = {}
    for _, row in subset.iterrows():
        record = row.to_dict()
        symbol = str(record["代码"])
        result[symbol] = {
            "symbol": symbol,
            "name": str(record["名称"]),
            "price": _to_float(record["最新价"]),
            "open": _to_float(record["今开"]),
            "high": _to_float(record["最高"]),
            "low": _to_float(record["最低"]),
            "change_pct": _to_float(record["涨跌幅"]),
            "amount": _to_float(record["成交额"]),
            "turnover_rate": _to_float(record.get("换手率")),
            "pe_ttm": _to_float(record.get("市盈率-动态")),
            "pb": _to_float(record.get("市净率")),
            "market_cap": _to_float(record.get("总市值")),
            "float_market_cap": _to_float(record.get("流通市值")),
        }
    return result


def _fetch_snapshot_tencent(symbols: list[str]) -> dict[str, dict[str, Any]]:
    query = ",".join(f"{_market_prefix(symbol)}{symbol}" for symbol in symbols)
    response = requests.get(f"https://qt.gtimg.cn/q={query}", timeout=15)
    response.raise_for_status()
    text = response.content.decode("gbk", errors="ignore")
    result: dict[str, dict[str, Any]] = {}

    for line in text.strip().split(";"):
        if "=\"" not in line:
            continue
        body = line.split("=\"", 1)[1].rsplit("\"", 1)[0]
        fields = body.split("~")
        if len(fields) < 47 or not fields[2]:
            continue
        symbol = fields[2]
        result[symbol] = _validate_snapshot({
            "symbol": symbol,
            "name": fields[1],
            "price": _to_float(fields[3]),
            "open": _to_float(fields[5]),
            "high": _to_float(fields[33]),
            "low": _to_float(fields[34]),
            "change_pct": _to_float(fields[32]),
            "amount": _to_float(fields[37]) * 10000,
            "turnover_rate": _to_float(fields[38]),
            "pe_ttm": _to_float(fields[39]),
            "market_cap": _to_float(fields[44]) * 100_000_000,
            "float_market_cap": _to_float(fields[45]) * 100_000_000,
            "pb": _to_float(fields[46]),
        })
    return result
