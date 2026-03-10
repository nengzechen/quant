# 项目架构说明

## 整体结构

```
stock_quant/
├── main.py                  # 应用入口（启动 FastAPI + 调度器）
├── server.py                # WSGI/ASGI 服务器配置
├── requirements.txt         # Python 依赖
├── pyproject.toml           # 项目元数据
│
├── api/                     # HTTP API 层（FastAPI）
│   ├── app.py               # FastAPI App 初始化、CORS、路由注册
│   ├── deps.py              # 依赖注入（配置、认证）
│   ├── middlewares/         # 中间件（JWT 认证、错误处理）
│   └── v1/
│       ├── router.py        # v1 路由聚合
│       ├── endpoints/       # 各业务路由
│       │   ├── analysis.py  # 个股分析
│       │   ├── stocks.py    # 实时行情 + 选股策略接口
│       │   ├── history.py   # 历史复盘
│       │   ├── backtest.py  # 回测
│       │   ├── agent.py     # AI Agent 对话
│       │   └── ...
│       └── schemas/         # Pydantic 请求/响应模型
│
├── src/                     # 核心业务逻辑
│   ├── config.py            # 全局配置（单例，读取 .env）
│   ├── analyzer.py          # LLM 驱动的股票分析器
│   ├── stock_analyzer.py    # 纯算法技术分析（MACD/RSI/均线）
│   ├── market_analyzer.py   # 大盘复盘（拉指数+新闻 → LLM → 报告）
│   ├── scheduler.py         # 定时任务调度
│   ├── storage.py           # SQLite 持久化
│   ├── formatters.py        # 输出格式化（Markdown/图片）
│   ├── notification.py      # 通知分发
│   │
│   ├── screening/           # 📊 量化选股引擎（新增）
│   │   ├── __init__.py      # 对外入口：Strategy1/Strategy2/run_*_batch
│   │   ├── indicators.py    # 指标模块池（7大类 30+ 原子函数）
│   │   └── screener.py      # 双策略引擎（强势突破 + 缠论抄底）
│   │
│   ├── core/                # 核心引擎组件
│   │   ├── pipeline.py      # 分析流水线
│   │   ├── market_profile.py# 市场画像（A股/美股/港股）
│   │   ├── market_review.py # 复盘数据结构
│   │   ├── market_strategy.py # 复盘策略模板
│   │   ├── backtest_engine.py # 回测引擎
│   │   └── trading_calendar.py # 交易日历
│   │
│   ├── agent/               # LLM Agent 对话系统
│   │   ├── llm_adapter.py   # litellm 统一接口
│   │   ├── executor.py      # Agent 执行器
│   │   ├── conversation.py  # 对话上下文管理
│   │   ├── tools/           # Agent 工具函数
│   │   └── skills/          # Agent 技能
│   │
│   ├── services/            # 业务服务层
│   │   ├── analysis_service.py  # 分析任务编排
│   │   ├── stock_service.py     # 股票数据服务
│   │   ├── backtest_service.py  # 回测服务
│   │   └── task_service.py      # 异步任务队列
│   │
│   ├── repositories/        # 数据访问层（SQLite）
│   │   ├── analysis_repo.py
│   │   ├── backtest_repo.py
│   │   └── stock_repo.py
│   │
│   └── notification_sender/ # 推送渠道
│       ├── telegram_sender.py
│       ├── feishu_sender.py
│       ├── discord_sender.py
│       └── ...（共10个渠道）
│
├── data_provider/           # 数据源适配层
│   ├── base.py              # 基类 + DataFetcherManager（多源路由）
│   ├── akshare_fetcher.py   # 东方财富（默认，免费）
│   ├── tushare_fetcher.py   # Tushare（需 Token）
│   ├── baostock_fetcher.py  # BaoStock（免费，备选）
│   ├── yfinance_fetcher.py  # Yahoo Finance（美股）
│   └── efinance_fetcher.py  # efinance
│
├── bot/                     # 消息机器人（多平台）
│   ├── dispatcher.py        # 命令分发器
│   ├── handler.py           # 消息处理器
│   ├── commands/            # 各指令处理（analyze/market/batch...）
│   └── platforms/           # 平台适配（钉钉/飞书/Discord）
│
├── quant/                   # 量化交易引擎（实盘/模拟盘）
│   ├── orchestrator.py      # 策略编排器
│   ├── broker/              # 券商接口（Futu/模拟盘）
│   ├── agents/              # 交易 Agent（信号聚合/风控/执行）
│   └── strategies/          # 交易策略（仓位管理等）
│
├── strategies/              # YAML 策略配置文件
│   ├── chan_theory.yaml      # 缠论策略
│   ├── bull_trend.yaml      # 多头趋势
│   └── ...（共11个策略模板）
│
├── apps/                    # 前端应用
│   ├── dsa-web/             # React Web 端（TypeScript + Vite）
│   └── dsa-desktop/         # Electron 桌面端
│
├── reports/                 # 自动生成的分析报告
│   ├── market_review/       # 每日大盘复盘（LLM 生成）
│   └── screening/           # 选股策略报告（算法生成）
│
├── docs/                    # 文档
│   ├── ARCHITECTURE.md      # 本文件（架构说明）
│   ├── DEPLOY.md            # 部署指南
│   ├── LLM_CONFIG_GUIDE.md  # LLM 配置说明
│   └── ...
│
├── tests/                   # 测试
├── scripts/                 # 构建/部署脚本
├── docker/                  # Docker 配置
└── patch/                   # 补丁（东方财富接口修复等）
```

---

## 数据流

```
用户请求 / 定时任务
      │
      ▼
  api/endpoints  或  scheduler
      │
      ▼
  src/services   ← 业务编排
      │
      ├── data_provider     ← 拉行情数据（akshare/tushare/...）
      │       │
      │       ▼
      │   日线/分时/资金/板块 等原始数据
      │
      ├── src/screening     ← 纯算法选股（0 token）
      │       │
      │       ├── indicators.py  计算30+指标
      │       └── screener.py    Strategy1/Strategy2 打分
      │
      └── src/analyzer      ← LLM 分析（消耗 token）
              │
              └── litellm → Gemini/DeepSeek/Anthropic
                      │
                      ▼
                  reports/ 或 推送通知
```

---

## 选股引擎详解（src/screening）

### 指标模块池（indicators.py）

| 分类 | 指标 |
|------|------|
| 市场情绪 | KDJ(大盘)、涨跌家数比 |
| 板块轮动 | 涨幅Top5板块、前五板块涨停家数 |
| 基本面 | PE区间、净利润同比连续增长 |
| 资金盘口 | 高开、强分时、量比、换手率、大单净流入、量能放大 |
| 传统技术 | 均线多头、MACD金叉>MA20、KDJ>50、DMI手拉手、头肩底 |
| 缠论特征 | 底分型、MACD底背离（日线/周线） |
| 特色主力 | 博弈长阳、九五之尊、CYS(<-15)、CD40(<-20) |

### 双策略引擎（screener.py）

**策略一：强势多头突破/接力**
- 适用场景：市场情绪好，追龙头
- 门槛：≥9分（满分18）
- 分层筛选：先快速技术预筛，再跑完整18维

**策略二：缠论深度抄底**
- 适用场景：标的大幅下跌，寻找底部
- 门槛：≥5分（满分6）
- 核心：MACD底背离 + 极度超跌（CYS+CD40）+ 底分型确认

---

## 不应提交到 Git 的文件

| 文件/目录 | 原因 | 处理方式 |
|-----------|------|----------|
| `.env` | 含 API Key/密钥 | .gitignore 已忽略 |
| `.venv/` | 虚拟环境 | .gitignore 已忽略 |
| `logs/` | 运行日志 | .gitignore 已忽略 |
| `data/*.db` | 数据库文件 | .gitignore 已忽略 |
| `AGENTS.md` | OpenClaw 本地配置 | 已从追踪移除 |
| `SKILL.md` | OpenClaw 技能文件 | 已从追踪移除 |
