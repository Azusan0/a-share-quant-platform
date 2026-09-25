# A-Share Quant Platform · A股量化辅助决策平台

一个跑在云端服务器上的**个人 A 股量化辅助决策系统**：从板块轮动发现、多源行情采集、盘中信号推送，到组合风控、盘后复盘、策略自评估与半自动调参迭代，形成一条完整的闭环。

> **免责声明**：本项目为个人工程实践与学习成果展示，**不构成任何投资建议**。系统只做信号发现与人工决策辅助，**不接入任何券商下单接口，不自动交易**。详见文末「实测结论」——**当前策略期望值为负，尚未跑正**，这正是本系统保留回测与迭代模块的原因。

---

## 目录

- [项目定位](#项目定位)
- [系统架构](#系统架构)
- [核心模块](#核心模块)
- [工程原则：全链路安全降级](#工程原则全链路安全降级)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [实测结论：策略自评估](#实测结论策略自评估)
- [数据脱敏说明](#数据脱敏说明)
- [技术栈](#技术栈)

---

## 项目定位

个人投资者盯盘的真实痛点不是「缺指标」，而是三件事：

1. **盘中信息过载**——5000+ 标的，人工不可能持续扫描；
2. **纪律会崩**——追高、扛单、该止损不止损；
3. **无法证伪**——凭感觉调参数，永远不知道策略到底赚不赚钱。

本系统针对性地做三件事：**用板块轮动把 5000 只标的收敛到可看的几个方向**、**用规则化风控（过热熔断/利空否决/止损止盈）替代盘中情绪**、**用回测 + 实盘流水自评估把「感觉」变成可量化的期望值**。

设计上刻意保持「人在环上」：系统只产信号、建议与提案，**所有下单动作由人执行**。

---

## 系统架构

```text
┌──────────────────────── 数据采集层 ────────────────────────┐
│ 动态板块池(sina/东财)  5分钟K线(腾讯/新浪多源)  基本面(质押/业绩) │
│ 市场情绪(财联社)      全球市场因子(美股/日经/韩综)  交易日历      │
└────────────────────────────┬───────────────────────────────┘
                             │  多源 fallback + 陈旧度校验
┌────────────────────────────▼───────────────────────────────┐
│                     决策引擎层                              │
│  方向轮动打分 → 过热熔断 → 大盘择时门槛 → 利空否决(AI复核)      │
│  → 入场信号 / 离场建议 / 影子推荐(推荐引擎 v7.4)               │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│                     输出与闭环层                            │
│  盘中提醒推送 · 组合监控 · HTTPS 只读仪表盘 · 日报上下文        │
│  信号流水 → T+1/T+3 回看 → 命中率统计 → 回测/迭代提案 → 人工确认 │
└────────────────────────────────────────────────────────────┘
```

整个系统由 **13 个 systemd service/timer** 驱动（见 `deploy/systemd/`），交易日按盘中节奏自动运行：影子扫描每 5 分钟、全球市场因子每 5 分钟、组合监控按交易时段、盘后 15:10 复盘、18:10 盘中数据回放审计、18:20 离场建议回放。

---

## 核心模块

### 数据与采集

| 模块 | 职责 |
|---|---|
| `data_source.py` | 实时快照（腾讯 `qt.gtimg.cn`）与历史日线多源获取，含 ETF 单位口径修正 |
| `intraday_data.py` | 5 分钟 K 线多源获取与 fallback（腾讯 ↔ 新浪） |
| `market_calendar.py` / `market_clock.py` | 交易日历与盘中时段判定 |
| `global_market_factor.py` | 美股/日经/韩综等外围因子采集，作为 A 股开盘前的外部环境输入 |
| `market_sentiment.py` | 市场情绪：涨跌家数、涨跌停分布、情绪分档 |
| `fundamental_data.py` | 基本面与风险：质押比例、业绩预告、商誉、解禁、负债率 |
| `board_strength.py` | 板块强度多维打分（广度、中位涨幅、资金净流入、持续性） |
| `dynamic_pool.py` | **动态板块池**：按强度自动进/出池，带替换边际与最小驻留时间，避免板块频繁抖动 |

### 策略与决策

| 模块 | 职责 |
|---|---|
| `strategies.py` | 策略核心：`direction_rotation`（方向轮动）、`pullback_buy`（回踩低吸）、`rebound_wrap`（反包）、`oversold_rebound`（超跌反弹） |
| `market_regime.py` | **大盘择时**：弱市自动抬高入场门槛、强制过热熔断，避免逆势追单 |
| `risk_veto.py` | **利空否决**：命中信号后查新闻/公告，确定性利空（立案/减持/预亏/退市）直接拦截降级；可选大模型二次复核，gpt→deepseek 兜底链 |
| `technical_diagnosis.py` | 个股技术面诊断 |
| `position_sizing.py` | 仓位计算 |
| `overheat` 相关逻辑（`strategies.py`） | 过热熔断：RSI/均线偏离/贴近日内高点三维判定，过热信号降级为观望 |

### 组合与风控

| 模块 | 职责 |
|---|---|
| `portfolio_store.py` | 多账户组合存储（SQLite），交易流水、持仓、自选，幂等写入 |
| `portfolio_monitor.py` | 盘中组合监控：止损/止盈/回撤离场提醒 |
| `portfolio_guard.py` | 组合护栏：同时活跃推荐上限、单板块上限、每日新增上限，按市场强弱动态收紧 |
| `position_advice.py` / `advice_card.py` | 动态离场建议与建议卡片 |
| `recommendation_engine.py` | 推荐引擎 v7.4：状态机（ignition/advance/…）驱动的影子推荐 |
| `recommendation_lifecycle.py` | 推荐生命周期管理 |
| `snapshot_store.py` | 5 分钟快照 SQLite 落库 |

### 评估与迭代（本项目的差异化部分）

| 模块 | 职责 |
|---|---|
| `signal_journal.py` | **信号自评估**：记录每次推送，事后回看 T+1/T+3 表现，算命中率与期望值 |
| `backtest.py` | **回测引擎**：建模 A 股现实（T+1 次日开盘买入、一字板买不进跳过、双边佣金+印花税+滑点全扣） |
| `iterator.py` | **半自动迭代器**：结合回测与实盘流水产出调参提案（方向层/策略层/仓位层/漂移层），**只提案不改配置** |
| `intraday_replay.py` | 盘中数据回放审计：校对覆盖率、重复、缺失与 fallback 一致性 |
| `journal_review.py` / `review_metrics.py` | 流水复盘与指标统计 |
| `data_quality.py` / `pipeline_status.py` | 数据质量与管线状态 |

### 界面与编排

| 模块 | 职责 |
|---|---|
| `dashboard_app.py` + `index.html` + `stock.html` | HTTPS 只读仪表盘（FastAPI），HTTP Basic + CSRF 双令牌 + 可信 Host 边界 + 登录失败限流 + 安全审计日志 |
| `intraday_pipeline.py` | 盘中影子管线编排：情绪 → 动态池 → 基本面 → 5分钟状态机 → 影子扫描 → 板块强度 → 推荐引擎 |
| `monitor.py` | 主监控循环（兼容旧版单标的模式） |
| `shadow_scanner.py` / `shadow_runner.sh` | 影子扫描（只记录不发提醒，用于验证策略而不影响实盘） |
| `delivery.py` | 提醒投递（stdout / jsonl） |
| `watchlist_cli.py` | 自选/持仓闭环 CLI |

---

## 工程原则：全链路安全降级

这是本项目最看重的工程约束——**任何一个外部依赖挂掉，都不能拖垮盘中主流程**：

- **数据源多级 fallback**：腾讯 → 新浪 → 东财，单源故障自动切换，并校验数据陈旧度；`intraday_replay.py` 每日盘后回放核对各源一致性。
- **模块级降级**：`market_regime` / `risk_veto` / `signal_journal` 任一异常（断网、模型超时、数据缺失）均安全降级放行，绝不影响主监控流程。
- **AI 复核兜底链**：大模型复核失败 → 退化为纯规则利空过滤；再失败 → 放行并记录。
- **影子模式**：`recommendation_engine` 以 `mode: shadow` 运行，只记录推荐不推送，验证充分后才考虑上线。
- **幂等与幂等键**：组合交易写入带 idempotency key，重复执行不会重复记账。
- **回测口径与实盘一致**：止损止盈参数与实盘同源，回测自动用 `volume × close` 反推成交额以对齐量能口径。

**51 个单元测试**覆盖策略量能口径、交易日历、组合存储与生命周期、CSRF/可信 Host、板块强度、盘中 fallback 演练等关键路径。

---

## 目录结构

```text
a-share-quant-platform/
├── a_share_alert_template/      # 核心代码（73 个模块，扁平结构，模块间直接 import）
│   ├── monitor.py               # 主监控循环
│   ├── intraday_pipeline.py     # 盘中管线编排
│   ├── strategies.py            # 策略核心
│   ├── dynamic_pool.py          # 动态板块池
│   ├── recommendation_engine.py # 推荐引擎
│   ├── portfolio_store.py       # 多账户组合存储
│   ├── dashboard_app.py         # 只读仪表盘后端
│   ├── index.html / stock.html  # 仪表盘前端
│   ├── backtest.py              # 回测引擎
│   ├── iterator.py              # 半自动迭代器
│   ├── signal_journal.py        # 信号自评估
│   ├── test_*.py                # 单元测试（21 个测试文件）
│   └── config.example.json      # 策略配置示例
├── runtime_scripts/             # 部署在调度目录的运行时脚本
│   ├── a_share_alert_monitor.py
│   ├── a_share_daily_review_context.py
│   └── a_share_positions.py
├── deploy/
│   ├── systemd/                 # 13 个 systemd unit（service + timer）
│   └── a-share-dashboard.env.example
├── examples/                    # 脱敏后的真实运行产物（见下方说明）
└── docs/                        # 架构与开发文档
```

---

## 快速开始

```bash
# 1. 环境：Python 3.11
python3.11 -m venv venv && source venv/bin/activate
pip install -r a_share_alert_template/requirements.lock

# 2. 跑单元测试（离线可跑，无需行情源）
cd a_share_alert_template && python -m pytest -q

# 3. 准备配置（把示例配置复制成自己的，填入真实路径与密钥）
cp examples/runtime_config.example.json my_config.json
#    编辑 my_config.json：
#    - risk_veto.llm_models[].api_key / base_url 填自己的（不填则自动跳过，等价于只用规则过滤）
#    - state_file / watchlist_file / dynamic_pool.file 等指向自己的数据目录

# 4. 跑一轮扫描（--ignore-session 允许非交易时段调试）
python monitor.py --config ../my_config.json --once --ignore-session

# 5. 跑回测，看策略到底有没有边缘
python backtest.py --config ../my_config.json --days 250 --out backtest_result.json

# 6. 生成调参提案（只提案，不自动改配置）
python iterator.py --config ../my_config.json --days 500 --out proposal.json
```

仪表盘：

```bash
cp deploy/a-share-dashboard.env.example /etc/a-share-dashboard.env
# 填入 DASHBOARD_USER / DASHBOARD_PASSWORD / DASHBOARD_TRUSTED_HOSTS 等
uvicorn dashboard_app:app --host 127.0.0.1 --port 18765
```

---

## 实测结论：策略自评估

本系统的信号自评估模块对 **2026-07-08 → 2026-09-22** 期间的推送做了 T+1/T+3 回看，共 **177 条已评估信号，覆盖 46 个方向**（数据见 `examples/signal_journal.sample.json`）：

| 策略 | 样本数 | 胜率 | 平均收益 | 平均盈利 | 平均亏损 | 盈亏比 |
|---|---:|---:|---:|---:|---:|---:|
| `direction_rotation` 方向轮动 | 66 | 42.4% | -0.40% | +5.14% | -4.48% | 1.15 |
| `pullback_buy` 回踩低吸 | 26 | 53.8% | -0.40% | +2.66% | -3.96% | 0.67 |
| `rebound_wrap` 反包 | 85 | 56.5% | -0.21% | +4.53% | -6.37% | 0.71 |
| **合计** | **177** | **50.8%** | **-0.31%** | — | — | **0.85** |

**结论：三条策略的期望值全部为负，整体盈亏比 0.85 不足以覆盖交易成本。**

这正是本项目最有价值的部分——它**用真实流水证伪了自己的策略**：

- 胜率 50.8% 看似「还行」，但盈亏比 0.85 意味着「赢的时候赚得少、亏的时候亏得多」，长期必然亏损；
- `direction_rotation` 盈亏比尚可（1.15）但胜率偏低，属于「高赔率低胜率」型，需要更严格的入场过滤；
- `rebound_wrap` 胜率最高（56.5%）却平均亏损最大（-6.37%），典型的「小赚大亏」，止损纪律需要收紧。

对应的迭代路径就是 `iterator.py` 的提案 → 人工审阅 → 回测验证 → 再上线，然后持续用 `signal_journal.py` 跟踪实盘与回测的漂移。

> 这也是我把系统保持为「影子推荐 + 人工下单」而非自动交易的原因：**在期望值跑正之前，任何自动化都只是加速亏损。**

---

## 数据脱敏说明

`examples/` 中的运行产物来自真实生产环境，但已做脱敏：

| 内容 | 处理方式 |
|---|---|
| 大模型 API Key / Base URL | 替换为 `PLACEHOLDER_*` 占位符 |
| 推送账号路由 ID | 替换为占位符 |
| 自选/持仓记录 | 替换为中性示例标的与示例成本价 |
| 信号流水的股票代码 | 匿名为 `S001` 形式，推送价归一化为 100 |
| 服务器 IP、私有代理域名 | 全部移除/替换为占位符 |

**保留的是**：真实的板块方向、策略标签、收益/回撤等统计口径、完整的数据结构，以及所有源码逻辑——保证示例可读、结构可信，同时不泄露任何凭证与真实持仓。

原文中的行情数据（板块涨跌幅、情绪指标、外围指数等）均为公开市场数据。

---

## 技术栈

- **语言**：Python 3.11
- **数据处理**：pandas / numpy / akshare
- **Web**：FastAPI + uvicorn（HTTPS 只读仪表盘）
- **存储**：SQLite（组合、快照、审计）
- **调度**：systemd service + timer
- **AI 赋能**：OpenAI 兼容接口做利空新闻二次复核（带多模型兜底链）
- **测试**：pytest / unittest，51 项测试

---

## 已知限制

- 入场候选池仍以人工维护的种子池 + 动态板块池为主，尚未做全市场龙头自动挖掘；
- 交易日历依赖外部数据源，源不可用时退化为「工作日 + 盘中时段」判断；
- 回测基于单一历史区间，存在风格切换风险；
- **策略期望值尚未跑正**（见上文实测结论），系统仍处于「影子验证 + 人工决策」阶段。
