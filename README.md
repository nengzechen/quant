<div align="center">

# A股量化选股系统

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 基于三阶段流水线的 A 股量化选股与模拟交易系统，每日盘前自动扫描全市场、盘中实时监控买入信号，配套 Web 界面实时查看选股结果与持仓。

</div>

---

## 架构总览

```
全市场 ~5500 只
      │
      ▼
┌─────────────────────────────────────┐
│  Phase 1  盘前扫描（08:00 北京时间）  │
│  s1 活跃池 / s2 超跌池 → 三大模型评分 │
│  输出：种子池 50~100 只              │
└──────────────────┬──────────────────┘
                   │
                   ▼
┌─────────────────────────────────────┐
│  Phase 2  盘中监控（09:30 开盘后）   │
│  实时行情 → 买入信号触发 → 推送通知   │
└──────────────────┬──────────────────┘
                   │
                   ▼
┌─────────────────────────────────────┐
│  Phase 3  AI 分析 + 量化执行        │
│  情感评分 → 风控过滤 → 模拟下单      │
└─────────────────────────────────────┘
```

详细指标文档：[docs/strategy_pipeline.md](docs/strategy_pipeline.md)

---

## 选股模型

| 模型 | 适用股票 | 核心逻辑 |
|------|----------|----------|
| **BottomSwing** 抄底波段 | s2 超跌池 | 超跌反弹：RSI 低位、成交量萎缩后放量、均线支撑 |
| **StrongTrend** 强势趋势 | s1 活跃池 | 主升浪：多头排列、MACD 金叉、成交量持续放大 |
| **LimitUpHunter** 涨停猎手 | s1 活跃池 | 涨停接力：板块联动、涨停强度、次日封板概率 |

每只股票经模型打分后，按板块各取 Top 5，最终合并为 50~100 只种子池。

---

## Web 界面

访问 `http://localhost:8000`，包含以下页面：

| 页面 | 路径 | 说明 |
|------|------|------|
| 首页 | `/` | 市场概览、今日分析摘要 |
| 选股监控 | `/screening` | Phase1 种子池实时展示，Phase2 触发状态，每 15 秒自动刷新 |
| 持仓管理 | `/portfolio` | 模拟账户（纸面交易），查看持仓盈亏、手动下单 |
| 问股 | `/chat` | AI 多轮策略问答（均线金叉、缠论、波浪等 11 种内置策略） |
| 回测 | `/backtest` | 历史策略回测 |
| 设置 | `/settings` | 配置管理、股票列表、API Key |

---

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+（前端构建）

### 本地运行

```bash
# 克隆项目
git clone https://github.com/nengzechen/quant.git && cd quant

# 安装 Python 依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM API Key 等

# 启动（自动编译前端 + 启动 Web 服务）
python main.py --webui
```

访问 `http://localhost:8000`

### 手动触发选股

```bash
# Phase1：全市场扫描，生成种子池（约 60 分钟）
python main.py --phase1 --phase1-target 100

# Phase2：盘中监控，330 轮 × 60 秒（覆盖全交易时段）
python main.py --phase2 --phase2-rounds 330 --phase2-interval 60
```

---

## 服务器部署

### 安装

```bash
pip install -r requirements.txt

# 构建前端
cd apps/dsa-web && npm install && npm run build && cd ../..

# 配置
cp .env.example .env
# 修改 WEBUI_HOST=0.0.0.0，WEBUI_ENABLED=true
```

### systemd 服务

```ini
# /etc/systemd/system/stock-quant.service
[Unit]
Description=Stock Quant Web Service
After=network.target

[Service]
WorkingDirectory=/opt/stock_quant
ExecStart=/opt/stock_quant/.venv/bin/python main.py --webui
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now stock-quant
```

### 定时任务

```bash
crontab -e
```

```cron
# UTC 时区（北京时间 = UTC+8）
# Phase1：08:00 北京时间扫描全市场（约 60 分钟）
0 0 * * 1-5 cd /opt/stock_quant && .venv/bin/python main.py --phase1 --phase1-target 100 >> logs/cron_phase1.log 2>&1

# Phase2：09:30 开盘后启动盘中监控
30 1 * * 1-5 cd /opt/stock_quant && .venv/bin/python main.py --phase2 --phase2-rounds 330 --phase2-interval 60 >> logs/cron_phase2.log 2>&1
```

---

## 主要配置项

`.env` 关键参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `WEBUI_ENABLED` | 启用 Web 界面 | `false` |
| `WEBUI_HOST` | 监听地址（外网访问改为 `0.0.0.0`） | `127.0.0.1` |
| `WEBUI_PORT` | 监听端口 | `8000` |
| `WEBUI_AUTO_BUILD` | 启动时自动构建前端 | `true` |
| `ADMIN_AUTH_ENABLED` | 启用登录鉴权 | `false` |
| `OPENAI_API_KEY` | LLM API Key（兼容 DeepSeek / 通义等） | — |
| `OPENAI_BASE_URL` | LLM API 地址 | — |
| `TUSHARE_TOKEN` | Tushare Pro Token | — |
| `AGENT_MODE` | 开启 Agent 策略问股 | `false` |

完整配置参考 `.env.example`。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.10、FastAPI、AKShare、LiteLLM |
| 前端 | React 18、TypeScript、Vite、Tailwind CSS |
| 数据 | AKShare（行情）、Tushare（财务）、AK 实时快照 |
| AI | 通过 LiteLLM 统一调用 OpenAI / Claude / Gemini / DeepSeek 等 |
| 模拟交易 | PaperBroker（本地 JSON 持久化） |

---

## 项目结构

```
.
├── main.py                      # 程序入口
├── src/
│   ├── screening/
│   │   ├── pipeline/
│   │   │   ├── phase1.py        # Phase1 全市场扫描
│   │   │   ├── phase2.py        # Phase2 盘中监控
│   │   │   └── seed_pool.py     # 种子池读写
│   │   ├── models/
│   │   │   ├── bottom_swing.py
│   │   │   ├── strong_trend.py
│   │   │   └── limit_up_hunter.py
│   │   └── indicators.py        # 技术指标计算
│   └── ...
├── api/v1/endpoints/
│   ├── screening.py             # 选股 API
│   ├── quant.py                 # 持仓 / 下单 API
│   └── ...
├── apps/dsa-web/                # React 前端
├── quant/broker/
│   └── paper_broker.py          # 模拟券商
├── data/
│   └── seed_pool_YYYYMMDD.json  # 每日种子池
├── docs/
│   └── strategy_pipeline.md     # 完整流水线文档
└── .env.example
```

---

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本项目产生的任何损失负责。

---

## License

[MIT License](LICENSE)

基于 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 二次开发。
