from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

STATE_PATH = Path('/root/.hermes/scripts/a_share_alert_runtime_state.json')
INDEX_CODES = {
    'sh000001': '上证指数',
    'sz399001': '深证成指',
    'sz399006': '创业板指',
    'sh000300': '沪深300',
    'sh000688': '科创50',
}
TARGET_SOURCES = [
    {'domain': 'cls.cn', 'name': '财联社', 'keyword': '收评'},
    {'domain': 'eastmoney.com', 'name': '东方财富', 'keyword': '收评'},
]
QUOTE_TIMEOUT = 15
SEARCH_TIMEOUT = 20
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'


def _fetch_tencent_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    url = 'https://qt.gtimg.cn/q=' + ','.join(codes)
    response = requests.get(url, timeout=QUOTE_TIMEOUT)
    response.raise_for_status()
    response.encoding = 'gbk'
    result: dict[str, dict[str, Any]] = {}
    for line in response.text.strip().split(';'):
        if '="' not in line:
            continue
        body = line.split('="', 1)[1].rsplit('"', 1)[0]
        fields = body.split('~')
        if len(fields) < 38 or not fields[2]:
            continue
        code = fields[2]
        try:
            price = float(fields[3]); prev_close = float(fields[4]); open_price = float(fields[5])
            high = float(fields[33]); low = float(fields[34]); amount_wan = float(fields[37])
        except Exception:
            continue
        change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
        result[code] = {
            'name': fields[1], 'price': price, 'prev_close': prev_close,
            'open': open_price, 'high': high, 'low': low,
            'change_pct': round(change_pct, 2), 'amount_wan': amount_wan, 'time': fields[30],
        }
    return result


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding='utf-8'))


def _summarize_state(state: dict[str, Any], target_date: str | None = None) -> dict[str, Any]:
    alerts = state.get('alerts', {}) if isinstance(state, dict) else {}
    observation = state.get('observation', {}) if isinstance(state, dict) else {}
    latest_alerts = []
    for key, payload in alerts.items():
        if not isinstance(payload, dict):
            continue
        last_signal = payload.get('last_signal') or {}
        latest_alerts.append({
            'key': key,
            'last_alert_at': payload.get('last_alert_at'),
            'direction': last_signal.get('direction'),
            'symbol': last_signal.get('symbol'),
            'name': last_signal.get('name'),
            'type': last_signal.get('type'),
            'score': last_signal.get('score'),
            'signal_type': last_signal.get('signal_type'),
            'strategy_label': last_signal.get('strategy_label') or last_signal.get('strategy'),
            'entry_reasons': last_signal.get('entry_reasons', []),
        })
    latest_alerts.sort(key=lambda item: item.get('last_alert_at') or '', reverse=True)

    # 只统计「当日」alert，避免跨日旧记录污染日报判断（审查项 J）。
    def _is_today(item: dict[str, Any]) -> bool:
        ts = item.get('last_alert_at') or ''
        return bool(target_date) and ts[:10] == target_date
    today_alerts = [a for a in latest_alerts if _is_today(a)] if target_date else latest_alerts

    direction_counter: dict[str, dict[str, int]] = {}
    for item in today_alerts:
        direction = item.get('direction') or '未知方向'
        bucket = direction_counter.setdefault(direction, {'seen': 0, 'emitted': 0})
        bucket['seen'] += 1
        bucket['emitted'] += 1
    top_directions = [
        {'direction': d, 'seen': s['seen'], 'emitted': s['emitted']}
        for d, s in direction_counter.items()
    ]
    top_directions.sort(key=lambda item: (item['emitted'], item['seen']), reverse=True)

    # 当日触发个股 Top 榜：只保留 entry 类，按评分排序，带代码/名称，供用户回复「加入自选 <名称>」。
    def _score_key(item: dict[str, Any]) -> float:
        try:
            return float(item.get('score'))
        except (TypeError, ValueError):
            return float('-inf')
    entry_today = [a for a in today_alerts if a.get('signal_type') in (None, 'entry')]
    entry_today.sort(key=_score_key, reverse=True)
    today_top = [
        {
            'rank': i + 1,
            'name': a.get('name'),
            'symbol': a.get('symbol'),
            'type': a.get('type'),
            'direction': a.get('direction'),
            'score': a.get('score'),
            'strategy_label': a.get('strategy_label'),
            'add_hint': f"加入自选 {a.get('name') or a.get('symbol')}",
        }
        for i, a in enumerate(entry_today[:8])
        if _score_key(a) != float('-inf')
    ]

    return {
        'last_seen_at': observation.get('last_seen_at'),
        'total_seen': observation.get('total_seen'),
        'total_emitted': observation.get('total_emitted'),
        'last_signal_sample': observation.get('last_signal_sample'),
        'top_directions': top_directions[:8],
        'latest_alerts': today_alerts[:8],
        'today_top': today_top,
        'today_top_hint': '以上为当日脚本触发的个股Top榜；如需跟踪某只，回复「加入自选 <名称或代码>」即可纳入盯盘与离场提醒。',
    }


def _extract_recap_titles(target_date: str) -> list[str]:
    query = quote(f'{target_date} A股 收评 收盘')
    url = f'https://html.duckduckgo.com/html/?q={query}'
    try:
        r = requests.get(url, timeout=SEARCH_TIMEOUT, headers={'user-agent': UA})
        r.raise_for_status()
        text = re.sub(r'<[^>]+>', '\n', r.text)
        lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
        good = []
        for line in lines:
            if any(bad in line for bad in ['展望', '上半年', '全年', '深度长文', 'DuckDuckGo']):
                continue
            if any(key in line for key in ['收评', '午评', '盘后', '截至收盘', '三大指数收跌', '三大指数收涨']):
                if line not in good:
                    good.append(line)
            if len(good) >= 6:
                break
        return good
    except Exception as exc:
        return [f'标题抓取失败：{exc}']


def _search_recap_links(target_date: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for query_text in [f'{target_date} A股 收评 收盘', f'{target_date} 三大指数 收跌 A股 收评', f'{target_date} 三大指数 收涨 A股 收评']:
        url = f'https://html.duckduckgo.com/html/?q={quote(query_text)}'
        try:
            r = requests.get(url, timeout=SEARCH_TIMEOUT, headers={'user-agent': UA})
            r.raise_for_status()
            for m in re.finditer(r'<a\s+rel="nofollow"\s+href="(https?[^"]+)"[^>]*class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL):
                raw_url = m.group(1)
                title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if 'eastmoney.com' in raw_url or 'cls.cn' in raw_url:
                    links.append({'url': raw_url, 'title': title, 'source': '东方财富' if 'eastmoney.com' in raw_url else '财联社'})
        except Exception:
            continue
    dedup, seen = [], set()
    for item in links:
        if item['url'] in seen:
            continue
        seen.add(item['url'])
        dedup.append(item)
    return dedup[:6]


def _fetch_url_content(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=25, headers={'user-agent': UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding or 'utf-8'
        text = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '\n', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 120 else None
    except Exception:
        return None


def _extract_turnover(text: str) -> str | None:
    for pat in [r'成交额[^0-9]{0,10}([0-9]+(?:\.[0-9]+)?)\s*万亿', r'两市[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)\s*万亿']:
        m = re.search(pat, text)
        if m:
            return f'{m.group(1)}万亿'
    return None


def _extract_breadth(text: str) -> dict[str, Any]:
    out = {'up': None, 'up_text': None, 'down': None, 'down_text': None}
    for pat, tmpl, key in [
        (r'全市场超([0-9]{3,5})只(?:个股)?上涨', '超{n}只上涨', 'up'),
        (r'全市场近([0-9]{3,5})只(?:个股)?上涨', '近{n}只上涨', 'up'),
        (r'上涨股票数量接近([0-9]{2,5})只', '接近{n}只上涨', 'up'),
        (r'全市场超([0-9]{3,5})只(?:个股)?下跌', '超{n}只下跌', 'down'),
        (r'全市场近([0-9]{3,5})只(?:个股)?下跌', '近{n}只下跌', 'down'),
        (r'下跌股票数量接近([0-9]{2,5})只', '接近{n}只下跌', 'down'),
    ]:
        m = re.search(pat, text)
        if m and out[key] is None:
            n = int(m.group(1))
            out[key] = n
            out[f'{key}_text'] = tmpl.format(n=n)
    return out


def _clean_phrase(line: str) -> str:
    line = re.sub(r'^(仅|逆市上涨|逆势走强|涨幅居前|跌幅居前|盘面上|板块方面|市场热点|回调，|表现疲弱|走弱，?)', '', line)
    line = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9、，, ]', '', line)
    line = re.sub(r'\s+', ' ', line).strip(' ，,。；;:：')
    parts = re.split(r'[，,、 ]+', line)
    parts = [p for p in parts if 1 < len(p) <= 8 and p not in {'板块', '方向', '个股', '市场', '上涨', '下跌', '逆市', '逆势', '跌幅居前', '涨幅居前'}]
    dedup = []
    for p in parts:
        if p not in dedup:
            dedup.append(p)
    return '、'.join(dedup[:4])


def _extract_main_weak(text: str) -> tuple[list[str], list[str]]:
    main_raw, weak_raw = [], []
    for pat in [r'仅[^。]{2,40}(?:板块)?逆市(?:上涨|走强)[^。]{0,30}', r'(?:涨幅居前|表现强势|走强).{0,8}[^。]{3,60}']:
        main_raw += [m.group(0).strip() for m in re.finditer(pat, text)]
    for pat in [r'[^。]{2,40}(?:板块)?跌幅居前[^。]{0,40}', r'沪指失守4000点[^。]{0,20}', r'(?:回调|走弱).{0,8}[^。]{3,60}']:
        weak_raw += [m.group(0).strip() for m in re.finditer(pat, text)]
    main = []
    for raw in main_raw:
        c = _clean_phrase(raw)
        if c:
            main.append(c)
    weak = []
    for raw in weak_raw:
        if '沪指失守4000点' in raw:
            weak.append('沪指失守4000点')
        else:
            c = _clean_phrase(raw)
            if c:
                weak.append(c)
    return list(dict.fromkeys(main))[:4], list(dict.fromkeys(weak))[:4]


def _generate_summary(turnover: str | None, breadth: dict[str, Any], main_lines: list[str], weak_lines: list[str]) -> str:
    parts = []
    if turnover:
        parts.append(f'成交额{turnover}')
    if breadth.get('up_text'):
        parts.append(breadth['up_text'])
    if breadth.get('down_text'):
        parts.append(breadth['down_text'])
    if main_lines:
        parts.append(f'相对强势：{"、".join(main_lines[:2])}')
    if weak_lines:
        parts.append(f'相对偏弱：{"、".join(weak_lines[:2])}')
    return '；'.join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description='A-share daily review context builder')
    parser.add_argument('--date', dest='target_date', help='Target trading date in YYYY-MM-DD for offline test')
    args = parser.parse_args()

    state = _load_state()
    target = args.target_date or datetime.now().strftime('%Y-%m-%d')
    state_summary = _summarize_state(state, target)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S CST')
    quotes = _fetch_tencent_quotes(list(INDEX_CODES.keys()))

    links = _search_recap_links(target)
    target_articles = []
    extracted = {'turnover': None, 'breadth_up': None, 'breadth_up_text': None, 'breadth_down': None, 'breadth_down_text': None, 'main_lines': [], 'weak_lines': [], 'summary': '', 'extraction_source': None}

    for link in links:
        content = _fetch_url_content(link['url'])
        if not content:
            continue
        target_articles.append({'url': link['url'], 'source': link['source'], 'title_sample': content[:80]})
        turnover = _extract_turnover(content)
        breadth = _extract_breadth(content)
        main_lines, weak_lines = _extract_main_weak(content)
        extracted = {
            'turnover': turnover,
            'breadth_up': breadth.get('up'),
            'breadth_up_text': breadth.get('up_text'),
            'breadth_down': breadth.get('down'),
            'breadth_down_text': breadth.get('down_text'),
            'main_lines': main_lines,
            'weak_lines': weak_lines,
            'summary': _generate_summary(turnover, breadth, main_lines, weak_lines),
            'extraction_source': link['source'],
        }
        if turnover or breadth.get('up') or main_lines:
            break

    payload = {
        'generated_at': now,
        'target_date': target,
        'mode': 'live',
        'scope': '当日事实预抓上下文；仅供当日A股日报使用；不得回溯历史会话充当事实来源',
        'indices': {label: quotes.get(code[2:]) or {'error': 'quote_unavailable'} for code, label in INDEX_CODES.items()},
        'market_stats': {
            'turnover': extracted.get('turnover'),
            'breadth_up': extracted.get('breadth_up'),
            'breadth_up_text': extracted.get('breadth_up_text'),
            'breadth_down': extracted.get('breadth_down'),
            'breadth_down_text': extracted.get('breadth_down_text'),
            'breadth_flat': None,
            'source': extracted.get('extraction_source') or 'none',
        },
        'script_state_summary': state_summary,
        'target_articles': target_articles,
        'extracted_data': extracted,
        'recap_titles': _extract_recap_titles(target),
        'hard_rules': [
            '只能使用本脚本输出、当天公开网页检索结果、以及当日状态文件作为事实依据',
            '不得引用 ~/.hermes/sessions 或任何旧会话缓存作为当日市场事实来源',
            '未核验到的北向、汇率、汇金、政策新增口径，直接省略，不要用宏观空话补位',
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
