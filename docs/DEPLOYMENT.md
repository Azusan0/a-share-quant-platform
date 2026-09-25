# 部署说明

本项目在生产环境以 **systemd service + timer** 驱动，不使用 crontab。交易日按盘中节奏自动运行。

## 运行时依赖

| 组件 | 版本 / 说明 |
|---|---|
| Python | **3.11**（`pyproject.toml` 约束 `>=3.11,<3.12`） |
| 依赖锁 | `a_share_alert_template/requirements.lock` |
| 外部数据源 | 腾讯财经、新浪财经、东方财富、财联社（均为公开行情接口，需外网访问） |
| 数据库 | SQLite（组合存储、5 分钟快照、安全审计） |
| 模型服务 | 可选，OpenAI 兼容 HTTP 接口（利空新闻二次复核） |

> ⚠️ 系统自带 `python3` 可能是 3.6，**必须使用 3.11 解释器**，否则 f-string / `from __future__` 等语法与依赖均不兼容。

## 目录约定

生产环境使用两个目录，本项目代码需相应落位：

```text
/usr/local/lib/hermes-agent/
├── venv/                          # Python 3.11 虚拟环境
└── a_share_alert_template/        # ← a_share_alert_template/ 目录内容

/opt/a-share-dashboard/            # ← 仪表盘运行目录（也可指向同一份代码）
```

`runtime_scripts/` 下的三个脚本按调度约定部署到运行时目录（本项目示例中为 `/root/.hermes/scripts/`）：

```text
/root/.hermes/scripts/
├── a_share_alert_monitor.py            # 主监控
├── a_share_daily_review_context.py     # 日报上下文
├── a_share_positions.py                # 自选/持仓 CLI
└── a_share_alert_runtime_config.json   # 运行时配置（含密钥，勿入库）
```

> 代码中的默认路径（如 `intraday_pipeline.py` 的 `DEFAULT_POOL`）按上述约定硬编码，迁移到其他路径时需同步调整。

## 环境准备

```bash
# 1. Python 3.11 虚拟环境
python3.11 -m venv /usr/local/lib/hermes-agent/venv
/usr/local/lib/hermes-agent/venv/bin/pip install -r a_share_alert_template/requirements.lock

# 2. 运行时配置（从示例复制后填入真实路径与密钥）
cp examples/runtime_config.example.json /root/.hermes/scripts/a_share_alert_runtime_config.json
chmod 600 /root/.hermes/scripts/a_share_alert_runtime_config.json

# 3. 仪表盘环境变量
cp deploy/a-share-dashboard.env.example /etc/a-share-dashboard.env
chmod 600 /etc/a-share-dashboard.env
```

## 安装 systemd 单元

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now \
  a-share-shadow.timer \
  a-share-portfolio-monitor.timer \
  a-share-global-market.timer \
  a-share-journal-review.timer \
  a-share-intraday-replay.timer \
  a-share-position-advice-replay.timer
systemctl enable --now a-share-dashboard.service
```

## 调度节奏

| 单元 | 触发 | 作用 |
|---|---|---|
| `a-share-shadow.timer` | 交易日每 5 分钟 | 影子信号扫描（只记录不推送） |
| `a-share-global-market.timer` | 交易日 05:00–15:00 每 5 分钟 | 外围市场因子采集 |
| `a-share-portfolio-monitor.timer` | 交易时段（09:15 起逐档） | 多账户组合监控与离场提醒 |
| `a-share-journal-review.timer` | 交易日 15:10 | 信号复盘与命中率统计 |
| `a-share-intraday-replay.timer` | 交易日 18:10 | 盘中数据回放审计 |
| `a-share-position-advice-replay.timer` | 交易日 18:20 | 动态离场建议回放 |
| `a-share-dashboard.service` | 常驻 | HTTPS 只读仪表盘 |

## 首次验证

```bash
TPL=/usr/local/lib/hermes-agent/a_share_alert_template
VENV=/usr/local/lib/hermes-agent/venv

# 1. 单元测试（离线可跑）
cd $TPL && $VENV/bin/python -m pytest -q

# 2. 语法自检
$VENV/bin/python -c "
import ast, pathlib
files = sorted(pathlib.Path('.').glob('*.py'))
[ast.parse(f.read_text(encoding='utf-8')) for f in files]
print('parsed', len(files), 'files OK')"

# 3. 跑一轮真实扫描
$VENV/bin/python monitor.py \
  --config /root/.hermes/scripts/a_share_alert_runtime_config.json \
  --once --ignore-session

# 4. 管线健康检查
cat /root/.hermes/scripts/a_share_pipeline_manifest.json
```

## 日志与排障

```bash
# 单个单元的最近日志
journalctl -u a-share-shadow.service -n 50 --no-pager

# 跟踪某个 timer 的下次触发
systemctl list-timers 'a-share-*'

# 管线状态
$VENV/bin/python $TPL/pipeline_status.py
```

### 常见「不是错误」的日志

以下均为外部数据不可用时的**安全降级**提示，不影响主流程：

- `trade calendar unavailable` — 交易日历源不可用，退化为「工作日 + 盘中时段」判断
- `market regime ... fallback neutral` — 大盘择时降级为中性，不加门槛
- `risk veto ... pass through` — 利空否决降级放行（模型或新闻源不可用）
- `pip install numpy` — 依赖缺失提示

## 安全注意

1. **配置文件含明文密钥**，必须 `chmod 600`，且绝不提交到版本库（`.gitignore` 已覆盖）。
2. 仪表盘务必配置 `DASHBOARD_TRUSTED_HOSTS` 与强 `DASHBOARD_PASSWORD`；生产环境建议加 TLS 并置于反向代理之后。
3. 建议为模型 key 设置额度上限与轮换周期。
4. 本系统**不接入券商下单接口**，所有交易由人工执行。
