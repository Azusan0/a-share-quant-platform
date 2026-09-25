#!/usr/bin/env python3
"""采集美日韩行情与全球资讯，并映射为A股板块/账户标的信息因子。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_global_market_factor.json")
DEFAULT_PORTFOLIO_DB = Path("/var/lib/a-share-dashboard/portfolios.db")
DEFAULT_MARKET_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")
DEFAULT_DYNAMIC_POOL = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
GLOBAL_INDEX_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/rank/indexRankDetail2"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
EASTMONEY_NEWS_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
WSCN_NEWS_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://stockapp.finance.qq.com/mstats",
}


INDEX_META = {
    "DJI": {"country": "美国", "display": "道琼斯"},
    "IXIC": {"country": "美国", "display": "纳斯达克"},
    "INX": {"country": "美国", "display": "标普500"},
    "N225": {"country": "日本", "display": "日经225"},
    "KS11": {"country": "韩国", "display": "韩国综合"},
    "XIN9": {"country": "中国离岸", "display": "富时中国A50"},
}

# 行业代理优先于宽基指数。代码格式沿用腾讯海外行情前缀，和 go-stock 的市场路由兼容。
PROXY_DEFS = [
    {"symbol": "usSOXX", "country": "美国", "sectors": {"半导体": 1.0, "人工智能/算力": .35}},
    {"symbol": "usSMH", "country": "美国", "sectors": {"半导体": 1.0, "人工智能/算力": .35}},
    {"symbol": "usNVDA", "country": "美国", "sectors": {"人工智能/算力": .8, "半导体": .45}},
    {"symbol": "usMU", "country": "美国", "sectors": {"存储芯片": .9, "半导体": .45}},
    {"symbol": "usTSM", "country": "美国", "sectors": {"半导体": .65}},
    {"symbol": "usTAN", "country": "美国", "sectors": {"光伏": 1.0}},
    {"symbol": "usLIT", "country": "美国", "sectors": {"锂电池": .9, "新能源车": .35}},
    {"symbol": "usTSLA", "country": "美国", "sectors": {"新能源车": .8, "机器人/自动化": .3}},
    {"symbol": "usXLE", "country": "美国", "sectors": {"油气": 1.0}},
    {"symbol": "usXLB", "country": "美国", "sectors": {"有色/化工": .9}},
    {"symbol": "usXLV", "country": "美国", "sectors": {"医药生物": 1.0}},
    {"symbol": "usXLF", "country": "美国", "sectors": {"金融": 1.0}},
    {"symbol": "usXLI", "country": "美国", "sectors": {"工业制造": .9, "机器人/自动化": .35}},
    {"symbol": "usITA", "country": "美国", "sectors": {"军工": 1.0}},
    {"symbol": "usGDX", "country": "美国", "sectors": {"黄金": 1.0, "有色/化工": .3}},
    {"symbol": "usKWEB", "country": "美国", "sectors": {"软件/互联网": .7, "传媒": .45}},
    {"symbol": "jp8035", "country": "日本", "sectors": {"半导体": .9}},
    {"symbol": "jp6857", "country": "日本", "sectors": {"半导体": .85, "人工智能/算力": .25}},
    {"symbol": "jp6723", "country": "日本", "sectors": {"半导体": .75, "汽车电子": .25}},
    {"symbol": "jp6758", "country": "日本", "sectors": {"消费电子": .8}},
    {"symbol": "jp7203", "country": "日本", "sectors": {"汽车": .85}},
    {"symbol": "jp9984", "country": "日本", "sectors": {"人工智能/算力": .45, "软件/互联网": .35}},
    {"symbol": "kr005930", "country": "韩国", "sectors": {"半导体": .75, "存储芯片": .65, "消费电子": .35}},
    {"symbol": "kr000660", "country": "韩国", "sectors": {"存储芯片": 1.0, "半导体": .65}},
    {"symbol": "kr051910", "country": "韩国", "sectors": {"锂电池": .75, "有色/化工": .25}},
    {"symbol": "kr006400", "country": "韩国", "sectors": {"锂电池": .85}},
    {"symbol": "kr005380", "country": "韩国", "sectors": {"汽车": .8, "新能源车": .3}},
]

INDEX_EXPOSURES = {
    "DJI": {"工业制造": .25, "金融": .2},
    "IXIC": {"半导体": .25, "人工智能/算力": .3, "软件/互联网": .25, "消费电子": .15},
    "INX": {"金融": .12, "医药生物": .1, "工业制造": .1},
    "N225": {"半导体": .18, "汽车": .2, "工业制造": .16, "消费电子": .12, "机器人/自动化": .12},
    "KS11": {"半导体": .2, "存储芯片": .28, "消费电子": .16, "锂电池": .14, "汽车": .1},
}

SECTOR_ALIASES = {
    "半导体": ("半导体", "芯片", "集成电路", "晶圆", "光刻", "封装测试", "先进封装"),
    "存储芯片": ("存储", "dram", "nand", "内存"),
    "人工智能/算力": ("人工智能", "算力", "ai", "服务器", "数据中心", "液冷", "cpo", "光模块"),
    "消费电子": ("消费电子", "手机", "面板", "显示", "元件", "苹果概念", "智能穿戴"),
    "软件/互联网": ("软件", "互联网", "云计算", "it服务", "网络安全", "游戏"),
    "传媒": ("传媒", "影视", "广告营销", "出版"),
    "光伏": ("光伏", "太阳能", "硅料", "硅片", "逆变器"),
    "锂电池": ("锂电", "电池", "正极", "负极", "电解液", "隔膜"),
    "新能源车": ("新能源车", "汽车零部件", "汽车电子", "充电桩"),
    "汽车": ("汽车整车", "乘用车", "商用车"),
    "汽车电子": ("汽车电子", "智能驾驶", "无人驾驶"),
    "机器人/自动化": ("机器人", "自动化", "工业母机", "减速器", "伺服"),
    "工业制造": ("机械设备", "工业", "制造", "工程机械", "电机"),
    "油气": ("石油", "油气", "天然气", "炼化"),
    "有色/化工": ("有色", "化工", "稀土", "铜", "铝", "小金属", "基础化学"),
    "黄金": ("黄金", "贵金属"),
    "医药生物": ("医药", "生物", "创新药", "医疗", "疫苗", "cro"),
    "金融": ("银行", "证券", "保险", "多元金融"),
    "军工": ("军工", "国防", "航空装备", "航天装备"),
}

# 只有跨境价格链条较直接的行业才把美日韩行情纳入持仓决策。
# 医药、金融、工业制造等以内需/国内政策为主，默认走国内因子，避免把海外波动硬套到个股。
GLOBAL_SENSITIVE_SECTORS = {
    "半导体", "存储芯片", "人工智能/算力", "消费电子", "软件/互联网", "传媒",
    "光伏", "锂电池", "新能源车", "汽车", "汽车电子", "机器人/自动化",
    "油气", "有色/化工", "黄金", "军工",
}

NEWS_KEYWORDS = {
    "半导体": ("半导体", "芯片", "晶圆", "英伟达", "台积电", "三星电子", "sk海力士", "美光"),
    "存储芯片": ("存储芯片", "dram", "nand", "海力士", "美光"),
    "人工智能/算力": ("人工智能", "ai", "算力", "英伟达", "数据中心", "服务器"),
    "消费电子": ("苹果", "iphone", "消费电子", "三星手机", "索尼"),
    "软件/互联网": ("软件", "云计算", "互联网", "微软", "谷歌", "亚马逊"),
    "光伏": ("光伏", "太阳能", "逆变器"),
    "锂电池": ("锂电", "电池", "碳酸锂"),
    "新能源车": ("电动车", "新能源汽车", "特斯拉"),
    "汽车": ("丰田", "现代汽车", "汽车销量"),
    "机器人/自动化": ("机器人", "自动化"),
    "油气": ("原油", "油价", "天然气", "opec"),
    "有色/化工": ("铜价", "铝价", "稀土", "化工"),
    "黄金": ("黄金", "金价"),
    "医药生物": ("医药", "药品", "fda", "生物科技"),
    "金融": ("银行", "金融", "美联储", "利率"),
    "军工": ("军工", "国防", "军费", "导弹"),
}

POSITIVE_WORDS = ("上涨", "大涨", "收涨", "涨超", "创新高", "增长", "超预期", "上调", "突破", "利好", "获批", "反弹")
NEGATIVE_WORDS = ("下跌", "大跌", "收跌", "跌超", "暴跌", "低于预期", "下调", "制裁", "限制", "禁运", "关税", "调查", "召回", "亏损", "冲突")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def fetch_global_indices(timeout: int = 12) -> dict[str, Any]:
    response = requests.get(GLOBAL_INDEX_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"腾讯全球指数返回异常: {payload.get('msg')}")
    return payload["data"]


def normalize_indices(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for region in ("common", "america", "asia", "europe", "other"):
        for raw in data.get(region) or []:
            code = str(raw.get("code") or "").strip()
            if code not in INDEX_META or code in rows:
                continue
            meta = INDEX_META[code]
            rows[code] = {
                "code": code, "qtcode": str(raw.get("qtcode") or ""),
                "name": str(raw.get("name") or meta["display"]), "display": meta["display"],
                "country": meta["country"], "price": _num(raw.get("zxj")),
                "change_pct": round(_num(raw.get("zdf")), 3),
                "state": str(raw.get("state") or "unknown").lower(), "region": region,
            }
    return [rows[code] for code in INDEX_META if code in rows]


def fetch_proxy_quotes(timeout: int = 12) -> str:
    symbols = ",".join(row["symbol"] for row in PROXY_DEFS)
    response = requests.get(TENCENT_QUOTE_URL + symbols, headers={**HEADERS, "Referer": "https://gu.qq.com/"}, timeout=timeout)
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


def parse_proxy_quotes(text: str) -> list[dict[str, Any]]:
    definitions = {row["symbol"]: row for row in PROXY_DEFS}
    result = []
    for symbol, payload in re.findall(r'v_([^=]+)="([^"]*)";', text):
        definition = definitions.get(symbol)
        values = payload.split("~")
        if not definition or len(values) < 33 or not values[1]:
            continue
        result.append({
            "symbol": symbol, "country": definition["country"], "name": values[1],
            "exchange_code": values[2], "price": _num(values[3]), "previous_close": _num(values[4]),
            "open": _num(values[5]), "volume": int(_num(values[6])), "quote_time": values[30],
            "change_pct": round(_num(values[32]), 3), "sectors": definition["sectors"],
        })
    return result


def fetch_eastmoney_news(limit: int = 40, timeout: int = 12) -> list[dict[str, Any]]:
    params = {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
              "pageSize": str(limit), "req_trace": str(uuid.uuid4())}
    response = requests.get(EASTMONEY_NEWS_URL, params=params,
                            headers={**HEADERS, "Referer": "https://kuaixun.eastmoney.com/"}, timeout=timeout)
    response.raise_for_status()
    rows = []
    for item in (response.json().get("data") or {}).get("fastNewsList") or []:
        title = str(item.get("title") or item.get("summary") or "").strip()
        if title:
            rows.append({"title": title, "summary": str(item.get("summary") or "")[:240],
                         "time": str(item.get("showTime") or ""), "source": "东方财富全球7x24"})
    return rows


def fetch_wscn_news(limit: int = 30, timeout: int = 12) -> list[dict[str, Any]]:
    params = {"channel": "us-stock-channel", "client": "pc", "limit": str(limit),
              "first_page": "true", "accept": "live,vip-live"}
    headers = {**HEADERS, "Referer": "https://wallstreetcn.com/", "x-client-type": "pc",
               "x-ivanka-app": "wscn|web|0.40.40|0.0|0"}
    response = requests.get(WSCN_NEWS_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 20000:
        raise RuntimeError(f"华尔街见闻快讯返回异常: {payload.get('message')}")
    rows = []
    for item in (payload.get("data") or {}).get("items") or []:
        title = str(item.get("title") or item.get("content_text") or "").strip()
        if not title:
            continue
        timestamp = int(_num(item.get("display_time")))
        rows.append({"title": title, "summary": str(item.get("content_text") or "")[:240],
                     "time": datetime.fromtimestamp(timestamp).isoformat(timespec="seconds") if timestamp else "",
                     "source": "华尔街见闻美股"})
    return rows


def _parse_news_time(value: str) -> datetime | None:
    value = str(value or "").strip().replace("Z", "+00:00")
    for candidate in (value, value.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except (TypeError, ValueError):
            pass
    return None


def normalize_news(rows: list[dict[str, Any]], now: datetime, max_age_hours: int = 18) -> list[dict[str, Any]]:
    result, seen = [], set()
    for row in rows:
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
        if not title:
            continue
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", title.lower())[:80]
        if key in seen:
            continue
        parsed = _parse_news_time(str(row.get("time") or ""))
        if parsed and parsed < now - timedelta(hours=max_age_hours):
            continue
        seen.add(key)
        text = (title + " " + str(row.get("summary") or "")).lower()
        sectors = [sector for sector, words in NEWS_KEYWORDS.items() if any(word.lower() in text for word in words)]
        positive = sum(word.lower() in text for word in POSITIVE_WORDS)
        negative = sum(word.lower() in text for word in NEGATIVE_WORDS)
        sentiment = 1 if positive > negative else -1 if negative > positive else 0
        result.append({**row, "title": title, "sectors": sectors, "sentiment": sentiment})
    return result[:60]


def score_sector_factors(indices: list[dict[str, Any]], proxies: list[dict[str, Any]],
                         news: list[dict[str, Any]], source_errors: int = 0) -> list[dict[str, Any]]:
    contributions: dict[str, list[dict[str, Any]]] = {}
    for row in indices:
        for sector, weight in INDEX_EXPOSURES.get(row["code"], {}).items():
            contributions.setdefault(sector, []).append({"name": row["display"], "country": row["country"],
                                                          "change_pct": row["change_pct"], "weight": weight,
                                                          "kind": "index"})
    for row in proxies:
        for sector, weight in row.get("sectors", {}).items():
            contributions.setdefault(sector, []).append({"name": row["name"], "country": row["country"],
                                                          "change_pct": row["change_pct"], "weight": weight,
                                                          "kind": "proxy"})
    news_by_sector: dict[str, list[dict[str, Any]]] = {}
    for item in news:
        for sector in item.get("sectors") or []:
            news_by_sector.setdefault(sector, []).append(item)
    rows = []
    for sector in sorted(set(contributions) | set(news_by_sector)):
        items = contributions.get(sector, [])
        weight_sum = sum(float(item["weight"]) for item in items)
        weighted_change = sum(float(item["change_pct"]) * float(item["weight"]) for item in items) / weight_sum if weight_sum else 0
        sector_news = news_by_sector.get(sector, [])[:5]
        news_sentiment = sum(int(item.get("sentiment") or 0) for item in sector_news) / len(sector_news) if sector_news else 0
        score = _clamp(50 + weighted_change * 8 + news_sentiment * 4, 0, 100)
        countries = sorted({str(item["country"]) for item in items})
        proxy_count = sum(item["kind"] == "proxy" for item in items)
        confidence = _clamp(30 + len(countries) * 10 + min(proxy_count, 6) * 6 + min(len(sector_news), 5) * 2 - source_errors * 5, 10, 95)
        strongest = sorted(items, key=lambda item: abs(float(item["change_pct"]) * float(item["weight"])), reverse=True)[:4]
        drivers = [f"{item['country']}{item['name']}{float(item['change_pct']):+.2f}%" for item in strongest]
        if sector_news:
            drivers.append(f"消息面{'偏多' if news_sentiment > .15 else '偏空' if news_sentiment < -.15 else '中性'}")
        rows.append({
            "sector": sector, "score": round(score, 1),
            "impact": "positive" if score >= 58 else "negative" if score <= 42 else "neutral",
            "weighted_change_pct": round(weighted_change, 3), "confidence": round(confidence),
            "countries": countries, "source_count": len(items), "drivers": drivers,
            "news": [{"title": item["title"], "time": item.get("time"), "source": item.get("source"),
                      "sentiment": item.get("sentiment", 0)} for item in sector_news[:3]],
        })
    return sorted(rows, key=lambda row: (abs(float(row["score"]) - 50), row["confidence"]), reverse=True)


def _market_summary(indices: list[dict[str, Any]]) -> dict[str, Any]:
    by_code = {row["code"]: row for row in indices}
    us = [by_code[code] for code in ("DJI", "IXIC", "INX") if code in by_code]
    asia = [by_code[code] for code in ("N225", "KS11") if code in by_code]
    us_change = sum(row["change_pct"] for row in us) / len(us) if us else 0
    asia_change = sum(row["change_pct"] for row in asia) / len(asia) if asia else 0
    us_score = _clamp(50 + us_change * 8, 0, 100)
    asia_score = _clamp(50 + asia_change * 8, 0, 100)
    global_score = us_score * .55 + asia_score * .45 if us and asia else us_score if us else asia_score
    return {
        "us_change_pct": round(us_change, 3), "us_score": round(us_score, 1),
        "us_state": "/".join(sorted({row["state"] for row in us})) if us else "missing",
        "asia_change_pct": round(asia_change, 3), "asia_score": round(asia_score, 1),
        "asia_state": "/".join(sorted({row["state"] for row in asia})) if asia else "missing",
        "global_score": round(global_score, 1),
        "risk_level": "risk_on" if global_score >= 58 else "risk_off" if global_score <= 42 else "neutral",
    }


def matched_sectors(labels: list[str]) -> list[str]:
    text = " ".join(str(label).lower() for label in labels if label)
    return [sector for sector, aliases in SECTOR_ALIASES.items() if any(alias.lower() in text for alias in aliases)]


def factor_for_stock(symbol: str, name: str, labels: list[str], snapshot: dict[str, Any]) -> dict[str, Any]:
    factors = {str(row.get("sector")): row for row in snapshot.get("sector_factors") or []}
    matched = [sector for sector in matched_sectors([name, *labels]) if sector in GLOBAL_SENSITIVE_SECTORS]
    rows = [factors[sector] for sector in matched if sector in factors]
    if not rows:
        return {"global_affected": False, "global_sector_score": 50, "global_impact": "neutral",
                "global_confidence": 0, "global_sectors": matched, "global_drivers": []}
    total_confidence = sum(max(float(row.get("confidence") or 0), 1) for row in rows)
    score = sum(float(row.get("score") or 50) * max(float(row.get("confidence") or 0), 1) for row in rows) / total_confidence
    confidence = max(float(row.get("confidence") or 0) for row in rows)
    drivers = list(dict.fromkeys(driver for row in rows for driver in row.get("drivers") or []))[:5]
    return {"global_affected": True, "global_sector_score": round(score, 1),
            "global_impact": "positive" if score >= 58 else "negative" if score <= 42 else "neutral",
            "global_confidence": round(confidence), "global_sectors": [row["sector"] for row in rows],
            "global_drivers": drivers}


def _local_stock_rows(portfolio_db: Path, market_db: Path, dynamic_pool: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    pool = _load(dynamic_pool)
    for sector in pool.get("active_sectors") or []:
        label = str(sector.get("direction") or sector.get("label") or "")
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol") or "")
            if symbol:
                rows.setdefault(symbol, {"symbol": symbol, "name": str(member.get("name") or symbol), "labels": []})["labels"].append(label)
    if portfolio_db.exists():
        try:
            connection = sqlite3.connect(f"file:{portfolio_db}?mode=ro", uri=True, timeout=3)
            for symbol, name in connection.execute("SELECT symbol,name FROM positions WHERE quantity>0 UNION SELECT symbol,name FROM watchlist_items"):
                rows.setdefault(str(symbol), {"symbol": str(symbol), "name": str(name or symbol), "labels": []})
            connection.close()
        except sqlite3.Error:
            pass
    if market_db.exists() and rows:
        try:
            connection = sqlite3.connect(f"file:{market_db}?mode=ro&immutable=1", uri=True, timeout=3)
            connection.row_factory = sqlite3.Row
            for symbol, item in rows.items():
                state = connection.execute("SELECT sector FROM intraday_states WHERE symbol=?", (symbol,)).fetchone()
                membership = connection.execute("SELECT concept_tags_json FROM stock_board_memberships WHERE symbol=? ORDER BY generated_at DESC LIMIT 1", (symbol,)).fetchone()
                # 状态表里的 sector 可能是旧动态池标签，不作为海外绑定依据。
                if membership:
                    item["labels"].extend(json.loads(membership["concept_tags_json"]))
            connection.close()
        except (sqlite3.Error, json.JSONDecodeError):
            pass
    for item in rows.values():
        item["labels"] = list(dict.fromkeys(label for label in item["labels"] if label))
    return rows


def _fetch_a_share_labels(symbol: str, timeout: int = 10) -> list[str]:
    secid = f"1.{symbol}" if symbol.startswith(("5", "6", "9")) else f"0.{symbol}"
    params = {"fltt": "2", "invt": "2", "secid": secid, "spt": "3", "pi": "0", "pz": "200", "po": "1", "fields": "f14"}
    response = requests.get("https://push2.eastmoney.com/api/qt/slist/get", params=params,
                            headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"}, timeout=timeout)
    response.raise_for_status()
    diff = (response.json().get("data") or {}).get("diff") or []
    items = diff.values() if isinstance(diff, dict) else diff
    return [str(item.get("f14") or "") for item in items if item.get("f14")]


def build_stock_bindings(snapshot: dict[str, Any], previous: dict[str, Any], portfolio_db: Path,
                         market_db: Path, dynamic_pool: Path, now: datetime) -> list[dict[str, Any]]:
    stocks = _local_stock_rows(portfolio_db, market_db, dynamic_pool)
    cached = {str(row.get("symbol")): row for row in previous.get("stock_bindings") or []}
    result = []
    for symbol, item in stocks.items():
        labels = list(item["labels"])
        old = cached.get(symbol) or {}
        try:
            old_at = datetime.fromisoformat(str(old.get("checked_at")))
        except ValueError:
            old_at = datetime.min
        if now - old_at < timedelta(hours=24):
            labels.extend(old.get("labels") or [])
        if not matched_sectors([item["name"], *labels]):
            try:
                labels.extend(_fetch_a_share_labels(symbol))
            except Exception:
                labels.extend(old.get("labels") or [])
            time.sleep(.12)
        labels = list(dict.fromkeys(label for label in labels if label))
        factor = factor_for_stock(symbol, item["name"], labels, snapshot)
        result.append({"symbol": symbol, "name": item["name"], "labels": labels[:30],
                       "checked_at": now.isoformat(timespec="seconds"), **factor})
    return sorted(result, key=lambda row: (row.get("global_affected", False), row["symbol"]), reverse=True)


def collect(now: datetime | None = None, previous: dict[str, Any] | None = None,
            portfolio_db: Path = DEFAULT_PORTFOLIO_DB, market_db: Path = DEFAULT_MARKET_DB,
            dynamic_pool: Path = DEFAULT_DYNAMIC_POOL) -> dict[str, Any]:
    now, previous = now or datetime.now(), previous or {}
    errors: dict[str, str] = {}
    try:
        indices = normalize_indices(fetch_global_indices())
    except Exception as exc:
        indices = previous.get("indices") or []
        errors["tencent_global_indices"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        proxies = parse_proxy_quotes(fetch_proxy_quotes())
    except Exception as exc:
        proxies = previous.get("proxies") or []
        errors["tencent_global_quotes"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    news_rows: list[dict[str, Any]] = []
    try:
        news_rows.extend(fetch_eastmoney_news())
    except Exception as exc:
        errors["eastmoney_global_news"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        news_rows.extend(fetch_wscn_news())
    except Exception as exc:
        errors["wscn_us_news"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    news = normalize_news(news_rows, now)
    if not news and previous.get("news"):
        news = normalize_news(previous["news"], now)
    factors = score_sector_factors(indices, proxies, news, len(errors))
    available_codes = {row["code"] for row in indices}
    missing_data = []
    if not {"DJI", "IXIC", "INX"}.issubset(available_codes):
        missing_data.append("美股三大指数")
    if "N225" not in available_codes:
        missing_data.append("日经225")
    if "KS11" not in available_codes:
        missing_data.append("韩国综合")
    payload = {
        "schema_version": 1, "generated_at": now.isoformat(timespec="seconds"),
        "providers": ["腾讯全球指数", "腾讯海外行情", "东方财富全球7x24", "华尔街见闻美股"],
        "indices": indices, "proxies": proxies, "news": news[:30], "sector_factors": factors,
        "market_summary": _market_summary(indices),
        "source_health": {"ok": len(errors) < 3 and bool(indices) and bool(proxies),
                          "errors": errors, "index_count": len(indices), "proxy_count": len(proxies),
                          "news_count": len(news)},
        "missing_data": missing_data,
    }
    payload["stock_bindings"] = build_stock_bindings(payload, previous, portfolio_db, market_db, dynamic_pool, now)
    return payload


def load_snapshot(path: Path = DEFAULT_OUTPUT, max_age_minutes: int = 20, now: datetime | None = None) -> dict[str, Any]:
    now, payload = now or datetime.now(), _load(path)
    try:
        generated = datetime.fromisoformat(str(payload.get("generated_at")))
        payload["stale"] = (now - generated).total_seconds() > max_age_minutes * 60
    except (TypeError, ValueError):
        payload["stale"] = True
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="美日韩行情与A股板块映射因子")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--portfolio-db", default=str(DEFAULT_PORTFOLIO_DB))
    parser.add_argument("--market-db", default=str(DEFAULT_MARKET_DB))
    parser.add_argument("--dynamic-pool", default=str(DEFAULT_DYNAMIC_POOL))
    parser.add_argument("--max-age-minutes", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output, now = Path(args.output), datetime.now()
    previous = _load(output)
    if not args.force:
        try:
            generated = datetime.fromisoformat(str(previous.get("generated_at")))
            if (now - generated).total_seconds() < args.max_age_minutes * 60:
                return 0
        except (TypeError, ValueError):
            pass
    payload = collect(now, previous, Path(args.portfolio_db), Path(args.market_db), Path(args.dynamic_pool))
    _atomic_json(output, payload)
    print(json.dumps({"generated_at": payload["generated_at"], "indices": len(payload["indices"]),
                      "proxies": len(payload["proxies"]), "factors": len(payload["sector_factors"]),
                      "bindings": len(payload["stock_bindings"]), "source_health": payload["source_health"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
