"""利空否决 / 否决式风控（#2）。

命中入场信号后、推送前，做一道「有没有重大利空」的校验。技术面再强，遇到立案、
减持、预亏、被问询等确定性利空就是接刀子。这是本项目最该落地的 AI 赋能点：
用大模型做**否决**（该不该拦），而不是让它预测涨跌。

两道防线，从确定到不确定：
  1) 规则硬过滤（rule_veto）：抓个股近 1-2 日新闻/公告标题，命中确定性利空关键词直接拦。
     纯脚本、零成本、盘中可跑，是主防线。
  2) LLM 复核（llm_veto，可选）：把标题喂给大模型做二次否决判断。
     多模型兜底链（gpt → deepseek → ...），任一挂了自动切下一个，全挂/未配置 → 跳过 LLM，
     只靠规则。**绝不因模型失效导致监控瘫痪或误拦。**

对外只暴露 assess_symbol()，返回 {veto: bool, level, reasons, source}。
调用方（monitor）在 veto=True 时把入场降级为观望，不直接买。
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import requests

# 确定性利空关键词（命中即硬否决）。这些是「基本面/合规」级别的，不是股价波动。
_HARD_VETO_KEYWORDS = [
    "立案", "调查", "被查", "问询函", "关注函", "警示函", "处罚", "违规",
    "退市", "*ST", "ST", "暂停上市", "终止上市",
    "预亏", "亏损", "业绩暴雷", "业绩变脸", "商誉减值", "计提减值",
    "减持", "清仓式减持", "大额减持", "股东减持", "高管减持",
    "解禁", "限售解禁", "大规模解禁",
    "诉讼", "仲裁", "冻结", "质押爆仓", "债务违约", "逾期",
    "造假", "财务造假", "内幕交易", "操纵",
]

# 减弱级关键词（不单独否决，供 LLM 参考 / 记录）。
_SOFT_FLAG_KEYWORDS = ["高位", "跳水", "闪崩", "利空", "承压", "下调评级", "看空"]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _fetch_news_titles(symbol: str, name: str, max_titles: int = 12) -> list[str]:
    """抓个股近日新闻/公告标题。多源尝试，失败返回空列表（不抛异常）。"""
    titles: list[str] = []

    # 源1：东方财富个股新闻（akshare 封装）
    try:
        import akshare as ak

        df = ak.stock_news_em(symbol=symbol)
        if df is not None and not df.empty:
            col = "新闻标题" if "新闻标题" in df.columns else df.columns[0]
            titles.extend(str(t) for t in df[col].head(max_titles).tolist())
    except Exception:
        pass

    if titles:
        return titles[:max_titles]

    # 源2：兜底——搜索引擎标题抓取（弱，仅在 akshare 不可用时）
    try:
        from urllib.parse import quote

        q = quote(f"{name} {symbol} 公告 利空")
        r = requests.get(f"https://html.duckduckgo.com/html/?q={q}", timeout=12, headers={"user-agent": _UA})
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", "\n", r.text)
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if name in line and len(line) <= 60:
                titles.append(line)
            if len(titles) >= max_titles:
                break
    except Exception:
        pass

    return titles[:max_titles]


def _rule_veto(titles: list[str]) -> dict[str, Any]:
    """规则硬过滤：命中确定性利空关键词即否决。"""
    hits: list[str] = []
    soft: list[str] = []
    for title in titles:
        for kw in _HARD_VETO_KEYWORDS:
            if kw in title:
                hits.append(f"{kw}（{title[:30]}）")
                break
        for kw in _SOFT_FLAG_KEYWORDS:
            if kw in title and title not in soft:
                soft.append(title[:30])
    return {
        "veto": bool(hits),
        "hits": hits[:5],
        "soft_flags": soft[:5],
    }


def _build_llm_prompt(name: str, symbol: str, titles: list[str]) -> str:
    joined = "\n".join(f"- {t}" for t in titles) or "（无可用标题）"
    return (
        f"你是A股风控助手。下面是股票 {name}({symbol}) 近日的新闻/公告标题。\n"
        f"请判断：作为**次日买入**的候选，是否存在应当【否决买入】的重大利空"
        f"（如立案调查、业绩暴雷、大额减持、限售解禁、退市风险、重大诉讼等）。\n"
        f"只看确定性的基本面/合规利空，不要因为单纯股价涨跌就否决。\n\n"
        f"标题列表：\n{joined}\n\n"
        f'只输出一行 JSON：{{"veto": true/false, "reason": "简短中文理由"}}'
    )


def _call_one_model(model_cfg: dict[str, Any], prompt: str) -> dict[str, Any] | None:
    """调用单个大模型。支持两种方式：command（本地命令）或 http（OpenAI 兼容接口）。

    返回 {veto, reason} 或 None（该模型不可用/失败，交由兜底链切下一个）。
    """
    kind = model_cfg.get("type", "http")
    try:
        if kind == "command":
            # 例如 Hermes 提供的本地 CLI：cmd 接受 stdin=prompt，stdout=模型回复
            cmd = model_cfg.get("command")
            if not cmd:
                return None
            proc = subprocess.run(
                cmd if isinstance(cmd, list) else [cmd],
                input=prompt, capture_output=True, text=True, timeout=int(model_cfg.get("timeout", 30)),
            )
            if proc.returncode != 0:
                return None
            return _parse_llm_output(proc.stdout)

        # http：OpenAI 兼容 chat/completions
        api_key = model_cfg.get("api_key", "")
        base_url = model_cfg.get("base_url", "")
        model_name = model_cfg.get("model", "")
        if not api_key or "PLACEHOLDER" in str(api_key) or not base_url:
            return None  # 未配置真实 key → 跳过，走兜底
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 150,
            },
            timeout=int(model_cfg.get("timeout", 30)),
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_output(content)
    except Exception:
        return None


def _parse_llm_output(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return {"veto": bool(obj.get("veto", False)), "reason": str(obj.get("reason", ""))}
    except Exception:
        return None


def _llm_veto(name: str, symbol: str, titles: list[str], veto_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """LLM 复核，带多模型兜底链。返回 {veto, reason, model} 或 None（全部不可用）。"""
    models = veto_cfg.get("llm_models", [])
    if not models:
        return None
    prompt = _build_llm_prompt(name, symbol, titles)
    for model_cfg in models:
        out = _call_one_model(model_cfg, prompt)
        if out is not None:
            out["model"] = model_cfg.get("name") or model_cfg.get("model") or model_cfg.get("type")
            return out
    return None  # 兜底链全挂 → 交回规则结论


def assess_symbol(symbol: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
    """对单个标的做利空否决评估。命中信号才调用（低频）。

    返回：
      veto: 是否否决买入
      level: clear / soft_warn / veto
      reasons: 理由列表
      source: rule / llm / rule+llm
    任何失败都不否决（返回 clear），把「拿不到信息」当作「不阻断」，避免误杀。
    """
    veto_cfg = config.get("risk_veto", {}) if isinstance(config, dict) else {}
    if not veto_cfg.get("enabled", True):
        return {"veto": False, "level": "disabled", "reasons": [], "source": "disabled"}

    titles = _fetch_news_titles(symbol, name)
    rule = _rule_veto(titles)

    # 规则命中确定性利空 → 直接否决，无需再问 LLM。
    if rule["veto"]:
        return {
            "veto": True,
            "level": "veto",
            "reasons": [f"确定性利空：{h}" for h in rule["hits"]],
            "source": "rule",
            "titles_seen": len(titles),
        }

    # 规则未命中 → 若配置了 LLM，做二次复核。
    llm = _llm_veto(name, symbol, titles, veto_cfg) if veto_cfg.get("use_llm", True) else None
    if llm is not None and llm.get("veto"):
        return {
            "veto": True,
            "level": "veto",
            "reasons": [f"模型判定利空：{llm.get('reason', '')}"],
            "source": f"llm:{llm.get('model')}",
            "titles_seen": len(titles),
        }

    reasons = []
    if rule["soft_flags"]:
        reasons = [f"弱提示（不否决）：{s}" for s in rule["soft_flags"]]
    return {
        "veto": False,
        "level": "soft_warn" if rule["soft_flags"] else "clear",
        "reasons": reasons,
        "source": "rule+llm" if llm is not None else "rule",
        "titles_seen": len(titles),
    }
