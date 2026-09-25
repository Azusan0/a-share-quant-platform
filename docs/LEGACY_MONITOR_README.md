# A-Share Alert Template

轻量 A 股盯盘提醒模板，目标是先把这四层稳定下来：

1. A 股实时数据获取
2. 买点策略接口
3. 去重与冷却机制
4. 消息提醒输出

当前版本已从“固定标的监控”升级为“方向发现优先”。它不再只盯几只 ETF，而是先从一组方向池中自动筛选今天更值得关注的方向，再在该方向内部挑出优先标的与备选。当前版本支持两种投递方式：标准输出 `stdout` 与落盘 `jsonl`，便于后续接入 Hermes `cronjob + send_message` 或独立守护进程。

## 目录结构

```text
a_share_alert_template/
├── README.md
├── config.example.json
├── monitor.py
├── data_source.py
├── delivery.py
├── strategies.py
└── state/
    └── .gitkeep
```

## 当前策略

### 1. `direction_rotation`

方向轮动发现器，适合“今天哪些方向可以上车”的场景。

工作方式：
- 先读取一组方向池
- 每个方向下同时挂 ETF 锚和少量核心股候选
- 先对候选成员逐个打分
- 在每个方向内选出优先标的和备选标的
- 只有当“方向强 + 标的入场触发成立”时才输出提醒

输出结果会告诉你：
- 哪个方向可以考虑
- 这个方向里优先看哪只 ETF 或核心股
- 该方向的 ETF 锚是谁
- 备选还有哪些 ETF / 核心股
- 触发理由是什么

### 2. `oversold_rebound`

保留旧的单标的增强版超跌反弹策略，兼容旧配置。

## 数据源策略

实时快照：
- 主：`requests.get("https://qt.gtimg.cn/q=...")`（15 秒超时，适合 cron 快照轮询）
- 备：腾讯 `qt.gtimg.cn`

历史日线：
- 主：`akshare.stock_zh_a_hist()`
- ETF 备：`akshare.fund_etf_hist_sina()`
- 通用备：`akshare.stock_zh_a_hist_tx()`

因此现在即便东方财富链路抽风，模板也不至于整轮扫描直接失败。

## 配置方式

复制一份配置：

```bash
cp a_share_alert_template/config.example.json a_share_alert_template/config.json
```

关键字段：

- `direction_pool`: 方向候选池
- `members`: 某个方向下的候选成员列表
- `type`: 候选成员类型，支持 `etf` / `stock`
- `strategy`: 策略参数
- `delivery`: 提醒输出方式
- `cooldown_minutes`: 同一方向提醒冷却时间
- `min_interval_seconds`: 两次轮询最小间隔

示例：

```json
{
  "direction_pool": [
    {
      "direction": "半导体",
      "members": [
        {"symbol": "512480", "name": "半导体ETF", "type": "etf"},
        {"symbol": "159813", "name": "半导体芯片ETF", "type": "etf"},
        {"symbol": "002371", "name": "北方华创", "type": "stock"},
        {"symbol": "688981", "name": "中芯国际", "type": "stock"}
      ]
    }
  ]
}
```

### `direction_rotation` 参数说明

- `history_bars`: 历史窗口长度
- `rsi_period`: RSI 周期
- `ma_period`: 短均线周期
- `momentum_period`: 动量观察周期
- `volume_ma_period`: 成交量均值窗口
- `amount_ma_period`: 成交额均值窗口
- `min_score`: 方向候选的最低得分
- `entry_min_score`: 标的进入可入场提醒的最低得分
- `entry_min_change_pct`: 当日涨跌幅门槛
- `entry_min_close_to_high_ratio`: 当前价需足够靠近日内高位
- `entry_min_volume_ratio`: 当前量能门槛
- `entry_min_amount_ratio`: 当前成交额门槛
- `entry_min_ma_bias_pct`: 站上短均线的最小偏离幅度
- `top_n`: 每次最多输出前几个方向
- `min_body_to_range_ratio`: 用来惩罚实体过弱、形态松散的标的

## 输出方式

### 1. stdout

`direction_rotation` 命中时会输出方向级 JSON，包含：
- `direction`: 可考虑的方向
- `best_member`: 该方向里优先关注的候选
- `type`: 当前优先候选是 `etf` 还是 `stock`
- `etf_anchor`: 该方向的 ETF 锚
- `backup_members`: 该方向里的备选成员
- `entry_reasons`: 入场理由

## 兼容性

- 若配置里仍是旧的 `watchlist`，系统会自动兼容转换为单标的池
- 若某个方向仍只写单个 `symbol`，系统也会自动兼容为单成员方向
- 若成员未写 `type`，默认按 `etf` 处理
- 若策略名为 `oversold_rebound`，仍按旧的单标的信号逻辑运行
- 当前 cron 监控可以直接切到 `direction_pool + direction_rotation`，不需要重建任务

## 持仓录入入口

提供了独立 CLI 脚本 `/root/.hermes/scripts/a_share_positions.py` 用于管理 `holding_pool`。

| 命令 | 说明 |
|------|------|
| `python3 a_share_positions.py help` | 查看帮助 |
| `python3 a_share_positions.py add <symbol> <name> [type] [quantity] [cost_price]` | 新增持仓 |
| `python3 a_share_positions.py list` | 列出全部持仓 |
| `python3 a_share_positions.py remove <symbol>` | 移除持仓 |

示例：
```bash
# 新增
python3 /root/.hermes/scripts/a_share_positions.py add 512480 半导体ETF etf 1000 1.200
python3 /root/.hermes/scripts/a_share_positions.py add 002371 北方华创 stock 200 180.50
# 列出
python3 /root/.hermes/scripts/a_share_positions.py list
# 移除
python3 /root/.hermes/scripts/a_share_positions.py remove 512480
```

该脚本直接读写 `/root/.hermes/scripts/a_share_alert_runtime_config.json` 中的 `holding_pool` 字段，可被 Hermes 聊天通过 `!terminal` 或 `cronjob` 触发。

## 当前限制

- 还没有直接调用 Hermes `send_message`，目前只做到"可桥接"
- 还没有交易日节假日精确判断，目前只做工作日 + 盘中时段判断
- 目前核心股候选仍是人工维护的小池子，不是自动全市场龙头挖掘

## 后续扩展建议

1. 增加 `hermes_message` 投递模式
2. 扩充更多方向的 ETF 锚与核心股种子池
3. 为核心股增加更严格的个股专用过滤
4. 从人工种子池进一步升级到自动板块/龙头发现
5. 持仓管理：支持追加/减仓数量，支持统一按 ETF 方向分组查询
