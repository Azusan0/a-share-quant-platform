from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import fcntl
from pathlib import Path
from typing import Any

ROOT = Path('/usr/local/lib/hermes-agent')
CONFIG = Path('/root/.hermes/scripts/a_share_alert_runtime_config.json')
LOCK = Path('/run/a_share_alert_monitor.lock')


LOCK.parent.mkdir(parents=True, exist_ok=True)
_lock_stream = LOCK.open('w', encoding='utf-8')
try:
    fcntl.flock(_lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    # 上一轮尚未结束：本轮安静退出，避免每 2 分钟叠加进程和请求。
    raise SystemExit(0)


def _to_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _fmt_pct(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return '-'
    return f"{num:.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    num = _to_float(value)
    if num is None:
        return '-'
    return f"{num:.{digits}f}"


def _fmt_time(value: Any) -> str:
    if not value:
        return '-'
    text = str(value)
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(text)
        return dt.strftime('%m-%d %H:%M:%S')
    except Exception:
        return text


def _fmt_candidate(item: dict[str, Any]) -> str:
    name = item.get('name') or item.get('symbol') or '未知标的'
    symbol = item.get('symbol', '-')
    raw_type = item.get('type', '-')
    item_type = '个股' if raw_type == 'stock' else 'ETF' if raw_type == 'etf' else str(raw_type)
    score = _fmt_num(item.get('score'), 1)
    change_pct = _fmt_pct(item.get('change_pct'))
    return f"{name}({symbol}, {item_type}) 评分{score}，涨幅{change_pct}"


def _classify_setup(best: dict[str, Any], anchor: dict[str, Any]) -> tuple[str, str]:
    strategy_label = best.get('strategy_label')
    if strategy_label == '回踩低吸':
        return ('回踩低吸型', '前面已有强势基础，当前更像回踩后的低吸观察点，适合分批而非追高')
    if strategy_label == '回踩反包':
        return ('回踩反包型', '前一段分歧后重新转强，适合看作二次上车点，但仍需防假修复')

    rsi = _to_float(best.get('rsi'))
    ma_bias = _to_float(best.get('ma_bias_pct'))
    close_to_high = _to_float(best.get('close_to_high_ratio'))
    anchor_change = _to_float(anchor.get('change_pct'))

    hot = False
    if rsi is not None and rsi >= 74:
        hot = True
    if ma_bias is not None and ma_bias >= 4.5:
        hot = True
    if close_to_high is not None and close_to_high >= 0.9:
        hot = True

    if hot:
        return ('观望型', '方向虽强，但个股已偏热，优先等回踩，不建议现价追单')
    if anchor_change is not None and anchor_change <= 0:
        return ('短线试错型', '个股强于板块，适合轻仓快进快出，不宜当中长线起点')
    return ('右侧突破型', '方向与个股同步偏强，更适合顺势观察，但仍不宜一次性重仓')


def _entry_band(best: dict[str, Any], setup_label: str) -> tuple[str, str]:
    price = _to_float(best.get('price'))
    ma_bias = _to_float(best.get('ma_bias_pct'))
    rsi = _to_float(best.get('rsi'))
    close_to_high = _to_float(best.get('close_to_high_ratio'))

    if price is None:
        return ('-', '现价数据异常，暂不生成区间')

    if setup_label == '观望型':
        low = price * 0.955
        high = price * 0.97
        return (f"{low:.2f} - {high:.2f}", '当前更适合等回落后再看承接，未回到区间前不建议追')

    if setup_label == '短线试错型':
        low = price * 0.975
        high = price * 0.99
        return (f"{low:.2f} - {high:.2f}", '若要参与，宜按短线轻仓试错处理，并控制追价幅度')

    if setup_label == '回踩低吸型':
        low = price * 0.99
        high = price * 1.01
        return (f"{low:.2f} - {high:.2f}", '更接近支撑附近观察区，可考虑小仓位分批，而不是追涨')

    if setup_label == '回踩反包型':
        low = price * 0.985
        high = price * 1.005
        return (f"{low:.2f} - {high:.2f}", '若次日仍稳在该区间上方，更像有效反包；跌回区间下沿则要谨慎')

    pullback_pct = 0.015
    if ma_bias is not None and ma_bias >= 2.5:
        pullback_pct = 0.02
    if rsi is not None and rsi >= 70:
        pullback_pct = max(pullback_pct, 0.02)
    if close_to_high is not None and close_to_high >= 0.85:
        pullback_pct = max(pullback_pct, 0.02)

    low = price * (1 - pullback_pct)
    high = price * (1 - max(pullback_pct - 0.01, 0.008))
    return (f"{low:.2f} - {high:.2f}", '更适合分批观察，不建议把现价当成唯一入场点')


def _horizon_note(setup_label: str) -> str:
    if setup_label == '观望型':
        return '当前更像短线过热段，不适合作为中长线直接起仓位'
    if setup_label == '短线试错型':
        return '偏短线交易结构，适合快进快出，不宜按中长线逻辑持有'
    if setup_label == '回踩低吸型':
        return '更接近回踩后的观察买点，适合轻仓试探，不宜一把重仓'
    if setup_label == '回踩反包型':
        return '更像强势股分歧后的二次上车点，适合短中线观察其延续性'
    return '更接近右侧确认买点，若参与宜分批，不宜追高满仓'


def _risk_note(best: dict[str, Any], anchor: dict[str, Any]) -> str:
    rsi = _to_float(best.get('rsi'))
    ma_bias = _to_float(best.get('ma_bias_pct'))
    anchor_change = _to_float(anchor.get('change_pct'))
    close_to_high = _to_float(best.get('close_to_high_ratio'))

    notes: list[str] = []
    if rsi is not None and rsi >= 74:
        notes.append('RSI偏高')
    if ma_bias is not None and ma_bias >= 4.5:
        notes.append('离短均线偏远')
    if close_to_high is not None and close_to_high >= 0.9:
        notes.append('接近日内高位')
    if anchor_change is not None and anchor_change <= 0:
        notes.append('方向锚未同步走强')
    if not notes:
        return '方向强于大盘，但仍需避免一次性重仓'
    return '、'.join(notes)


def _format_entry_alert(alert: dict[str, Any]) -> str:
    direction = alert.get('direction', '未知方向')
    best = alert.get('best_member') or {}
    anchor = alert.get('etf_anchor') or {}
    backups = alert.get('backup_members') or []
    reasons = alert.get('entry_reasons') or best.get('entry_reasons') or []
    reentry_type = alert.get('reentry_type') or best.get('reentry_type')
    reentry_reason = alert.get('reentry_reason') or best.get('reentry_reason')
    setup_label, setup_desc = _classify_setup(best, anchor)
    band, band_note = _entry_band(best, setup_label)
    horizon_note = _horizon_note(setup_label)
    risk_note = _risk_note(best, anchor)

    strategy_label = alert.get('strategy_label') or best.get('strategy_label') or '右侧突破'
    title = '【A股二次机会提醒】' if reentry_type == 'second_chance' else f"【{strategy_label}提醒】"
    lines = [
        f"{title}{direction}",
        f"优先标的：{_fmt_candidate(best or alert)}",
    ]

    price = best.get('price', alert.get('price'))
    if price is not None:
        lines.append(f"现价：{_fmt_num(price, 3)}")

    if reentry_reason:
        lines.append(f"机会性质：{reentry_reason}")

    lines.append(f"交易建议：{setup_label}")
    lines.append(f"建议说明：{setup_desc}")
    lines.append(f"持仓视角：{horizon_note}")
    lines.append(f"参考入场区间：{band}")
    lines.append(f"区间说明：{band_note}")

    if reasons:
        lines.append(f"触发原因：{'、'.join(str(x) for x in reasons)}")

    # 止损/目标位（#1 闭环）：加入自选后离场逻辑据此盯盘。
    stop_loss = _to_float(alert.get('stop_loss'))
    take_profit = _to_float(alert.get('take_profit'))
    if stop_loss is not None or take_profit is not None:
        parts = []
        if stop_loss is not None:
            parts.append(f"止损参考{_fmt_num(stop_loss, 3)}")
        if take_profit is not None:
            parts.append(f"目标参考{_fmt_num(take_profit, 3)}")
        lines.append("；".join(parts))

    lines.append(f"风险提示：{risk_note}")
    lines.append("如需跟踪该标的，回复「加入自选 " + str(alert.get('name') or alert.get('symbol') or '') + "」纳入盯盘。")

    if anchor:
        lines.append(f"方向锚：{_fmt_candidate(anchor)}")

    if backups:
        valid_backups = [item for item in backups if _to_float(item.get('score')) is not None]
        backup_text = '；'.join(_fmt_candidate(item) for item in valid_backups[:3])
        if backup_text:
            lines.append(f"备选观察：{backup_text}")

    if alert.get('time'):
        lines.append(f"时间：{_fmt_time(alert['time'])}")

    return '\n'.join(lines)


def _format_exit_alert(alert: dict[str, Any]) -> str:
    name = alert.get('name') or alert.get('symbol') or '未知标的'
    symbol = alert.get('symbol', '-')
    item_type = alert.get('type', '-')
    exit_reasons = alert.get('exit_reasons') or []
    lines = [
        f"【A股离场提醒】{name}({symbol}, {item_type})",
    ]
    current_price = alert.get('current_price', alert.get('price'))
    if current_price is not None:
        lines.append(f"现价：{_fmt_num(current_price, 3)}")
    pnl_pct = _to_float(alert.get('pnl_pct'))
    if pnl_pct is not None:
        lines.append(f"浮盈亏：{pnl_pct:.2f}%")
    if exit_reasons:
        lines.append(f"触发原因：{'、'.join(str(x) for x in exit_reasons)}")
    if alert.get('time'):
        lines.append(f"时间：{_fmt_time(alert['time'])}")
    return '\n'.join(lines)


def _format_watch_alert(alert: dict[str, Any]) -> str:
    direction = alert.get('direction', '未知方向')
    name = alert.get('name') or alert.get('symbol') or '未知标的'
    symbol = alert.get('symbol', '-')
    veto = alert.get('veto_reasons') or []
    hot = alert.get('overheat_reasons') or []

    if veto:
        # 利空观望：技术面触发但检测到基本面/合规利空，建议回避。
        lines = [
            f"【利空观望提醒】{direction}",
            f"标的：{name}({symbol})",
            "结论：技术面虽触发，但检测到潜在利空，建议回避、不参与",
            f"利空信号：{'、'.join(str(x) for x in veto)}",
        ]
        source = alert.get('veto_source')
        if source:
            lines.append(f"判定来源：{source}")
    else:
        # 过热观望：方向偏强但短线过热，等回踩。
        lines = [
            f"【过热观望提醒】{direction}",
            f"标的：{name}({symbol})",
            "结论：方向偏强但已过热，建议等回踩，不宜现价追高",
        ]
        if hot:
            lines.append(f"过热信号：{'、'.join(str(x) for x in hot)}")
    if alert.get('time'):
        lines.append(f"时间：{_fmt_time(alert['time'])}")
    return '\n'.join(lines)


def _format_alert_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ''
    try:
        alert = json.loads(line)
    except json.JSONDecodeError:
        return line

    signal_type = alert.get('signal_type')
    if signal_type == 'entry':
        return _format_entry_alert(alert)
    if signal_type == 'exit':
        return _format_exit_alert(alert)
    if signal_type == 'watch':
        return _format_watch_alert(alert)
    return alert.get('message') or line


cmd = [
    sys.executable,
    str(ROOT / 'a_share_alert_template' / 'monitor.py'),
    '--config',
    str(CONFIG),
    '--once',
    '--cron-quiet',
]

TRANSIENT_ERROR_MARKERS = (
    'timed out',
    'timeout',
    'read timed out',
    'connection reset',
    'connection aborted',
    'temporarily unavailable',
    'temporary failure',
    'name or service not known',
    'failed to establish a new connection',
    'max retries exceeded',
    '502 bad gateway',
    '503 service unavailable',
    '504 gateway timeout',
    'too many requests',
)


def _looks_transient_error(stderr_text: str) -> bool:
    text = (stderr_text or '').strip().lower()
    if not text:
        return False
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


process = subprocess.Popen(
    cmd,
    cwd=str(ROOT),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    start_new_session=True,
)
try:
    stdout_text, stderr_text = process.communicate(timeout=90)
except subprocess.TimeoutExpired:
    # 杀掉整个进程组，避免数据源请求线程脱离父进程后长期残留。
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
    raise SystemExit(0)

result = subprocess.CompletedProcess(cmd, process.returncode, stdout_text, stderr_text)

if result.returncode != 0:
    stderr_text = result.stderr or ''
    if _looks_transient_error(stderr_text):
        raise SystemExit(0)
    sys.stderr.write(stderr_text or 'monitor failed\n')
    raise SystemExit(result.returncode)

stdout = (result.stdout or '').strip()
if stdout:
    formatted = [text for text in (_format_alert_line(line) for line in stdout.splitlines()) if text]
    if formatted:
        print('\n\n'.join(formatted))
