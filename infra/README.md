# 📦 infra/ — MySQL 本地数据库运维手册

本目录包含 MySQL Docker 容器的**一键部署、状态检查、每日更新、备份/恢复**全套自动化脚本。

---

## 目录结构

```
infra/
├── README.md       ← 本文件（运维手册）
├── deploy.sh       ← 一键部署 MySQL Docker
├── status.sh       ← 查看数据库状态与统计
├── update.sh       ← 每日 K 线数据增量更新
├── backup.sh       ← 备份数据库到本地文件
└── restore.sh      ← 从备份文件恢复数据库

相关文件（本目录外）：
  db/schema.sql               ← 建库 DDL（4张表 + 1个视图）
  docker/docker-compose.yml   ← Docker Compose 配置（含 MySQL 服务）
  scripts/download_kline.py   ← K 线数据采集主脚本
  .env                        ← 数据库连接配置
```

---

## 快速上手

### 前置要求

| 软件 | 最低版本 | 说明 |
|------|---------|------|
| Docker Desktop | 24.x | [下载地址](https://docs.docker.com/get-docker/) |
| Python | 3.10+ | 含 `pymysql`, `sqlalchemy`, `akshare` |
| 磁盘空间 | 2 GB | MySQL 数据 + 日志 |

```bash
# 安装 Python 依赖
pip install pymysql sqlalchemy akshare
```

### 第一次使用（3步完成）

```bash
# Step 1: 部署 MySQL 容器（约 2 分钟）
bash infra/deploy.sh

# Step 2: 全量下载历史 K 线（约 20 分钟，5000+ 只股票，246万行）
bash infra/deploy.sh --full
# 或者：bash infra/update.sh --full

# Step 3: 验证数据
bash infra/status.sh
```

---

## 脚本详解

### 🚀 deploy.sh — 部署 MySQL Docker

启动 `quant-mysql` 容器，等待健康检查通过后可选执行数据下载。

```bash
bash infra/deploy.sh              # 只部署容器（不下载数据）
bash infra/deploy.sh --full       # 部署 + 首次全量下载（≈20分钟）
bash infra/deploy.sh --update     # 部署 + 今日增量更新（≈3分钟）
```

**内部执行流程：**
1. 检查 Docker 是否运行
2. 检查/创建 `.env` 配置文件
3. `docker pull mysql:latest`（首次）
4. `docker compose up -d mysql`
5. 等待 healthcheck 通过（最多 60 秒）
6. 验证连接并显示统计信息
7. （可选）调用 `scripts/download_kline.py` 下载数据

---

### 📊 status.sh — 数据库状态检查

```bash
bash infra/status.sh              # 完整报告（容器 + 数据统计 + 缺口检测）
bash infra/status.sh --quick      # 快速版（只看容器 + 行数）
bash infra/status.sh --gaps       # 只检查数据缺口
```

**输出内容：**

```
════════════════════════════════════════════════════════
  A股量化系统 — MySQL 数据库状态报告
  2026-03-17 19:15:30
════════════════════════════════════════════════════════

1. Docker 容器
──────────────────────────────────────────────────────
[✓]    容器状态: running
[✓]    健康检查: healthy ✓
       镜像:     mysql:latest
       运行时长: Up 2 hours

2. MySQL 连接
──────────────────────────────────────────────────────
[✓]    MySQL 连接正常
       版本:     MySQL 9.x.x
       地址:     quant@127.0.0.1:3306/quant

3. 数据统计
──────────────────────────────────────────────────────
       stock_info（股票基础信息）:
         总数:   5310 只
         可交易: 5013 只（非ST、非北交所）
       kline_daily（日线数据）:
         总行数:   2,461,010 条
         股票数:   5006 只
         交易日数: 491 天
         数据范围: 2024-03-18  ~  2026-03-17
[✓]    最新数据: 2026-03-17（今日，4987 只）
```

---

### 🔄 update.sh — 每日增量更新

每个交易日**收盘后 15:30** 运行，只补充当日新数据，通常约 3 分钟。

```bash
bash infra/update.sh              # 增量更新今日数据
bash infra/update.sh --full       # 全量重新下载（500个交易日）
bash infra/update.sh --codes 600519,000001  # 只更新指定股票
bash infra/update.sh --workers 5  # 并发5线程（默认3）
```

**配置 crontab 自动运行：**

```bash
crontab -e
```

添加以下内容（每天周一至周五 15:35 自动更新）：

```cron
35 15 * * 1-5 cd /path/to/quant && bash infra/update.sh >> logs/cron_update.log 2>&1
```

---

### 💾 backup.sh — 数据库备份

```bash
bash infra/backup.sh              # 备份到 backups/ 目录（gzip压缩）
bash infra/backup.sh --dir /mnt/backup  # 备份到指定目录
bash infra/backup.sh --keep 7     # 保留最近7份（默认5份）
bash infra/backup.sh --no-compress  # 不压缩，输出.sql文件
```

备份文件命名格式：`backups/quant_YYYYMMDD_HHMMSS.sql.gz`

典型备份大小：~150 MB（246万行，gzip压缩后）

**建议备份策略：**
- 每周日全量备份一次（手动）
- 每日增量更新后自动备份（update.sh 已内置触发选项）

---

### ♻️ restore.sh — 数据恢复

```bash
bash infra/restore.sh backups/quant_20260317_153000.sql.gz
bash infra/restore.sh --latest    # 自动恢复最新备份
bash infra/restore.sh backups/quant_20260317_153000.sql.gz --no-pre-backup
```

> ⚠️ 恢复会**覆盖**当前数据库，脚本会先自动备份现有数据（除非加 `--no-pre-backup`）并要求二次确认。

---

## 数据库设计

### 表结构概览

```
quant（数据库）
├── stock_info          — 股票基础信息（5310 条）
│     code, name, market, board, sector, is_st, is_active
│
├── kline_daily         — 日线行情（246万行）
│     code, trade_date, OHLCV, pct_chg, turnover, amplitude
│     MA5/10/20/60, vol_ma5, source
│     UNIQUE KEY: (code, trade_date)
│
├── fundamentals        — 基本面数据（按需更新）
│     code, report_date, pe_ttm, pb, total_mv, profit_yoy
│
├── download_progress   — 下载进度（断点续跑）
│     code, last_date, total_rows, status, error_msg
│
└── v_latest_kline      — 视图：最新一日行情（JOIN stock_info）
```

### 三级数据优先级

系统在计算指标时按以下优先级读取数据：

```
1. 本地 MySQL（最快，<1ms）
       ↓ 失败
2. akshare 新浪接口（网络请求，~200ms）
       ↓ 失败
3. baostock（备用接口，~300ms）
```

数据在内存中缓存 TTL=300s，避免同一股票重复查询。

---

## 环境配置（.env）

```ini
# MySQL 数据库连接
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=quant
MYSQL_PASS=quant123
MYSQL_DB=quant
```

所有脚本都会自动读取项目根目录的 `.env` 文件。

---

## 常见问题

### Q: 容器启动失败，提示端口占用

```bash
# 检查 3306 端口占用
lsof -i :3306

# 修改端口（.env）
MYSQL_PORT=3307
```

### Q: 数据下载中途中断，如何续跑

```bash
# 直接重新运行，会自动跳过已下载股票
bash infra/update.sh --full
```

进度通过 `download_progress` 表跟踪，`status='ok'` 的股票会被跳过。

### Q: 如何迁移数据到新机器

```bash
# 旧机器：备份
bash infra/backup.sh

# 新机器：部署 + 恢复
bash infra/deploy.sh
bash infra/restore.sh backups/quant_YYYYMMDD_HHMMSS.sql.gz
```

### Q: 想手动连接 MySQL

```bash
# 通过 Docker 连接
docker exec -it quant-mysql mysql -u quant -pquant123 quant

# 直接连接（需要 mysql 客户端）
mysql -h 127.0.0.1 -P 3306 -u quant -pquant123 quant
```

### Q: 删除所有数据重新开始

```bash
# 停止并删除容器 + 数据卷（⚠️ 不可恢复）
docker compose -f docker/docker-compose.yml down -v

# 重新部署
bash infra/deploy.sh --full
```

---

## 性能数据

| 操作 | 耗时 | 说明 |
|------|------|------|
| 容器启动 | ~30秒 | 含健康检查 |
| 全量下载 | ~20分钟 | 5006只 × 500天 = 246万行，3并发 |
| 增量更新 | ~3分钟 | 每日新增 ~5000 条 |
| Phase1 扫描 | **~10分钟** | 5013只全量评分（MySQL本地读取） |
| 备份（gzip） | ~30秒 | 246万行 → ~150MB |
| 恢复 | ~2分钟 | 从 .sql.gz 恢复 |

> 对比：使用实时 HTTP 接口时，Phase1 扫描需要 **2.5 小时**；改用 MySQL 后降至 **10 分钟**（提速 15x）。

---

## 相关文档

- [Docker Compose 配置](../docker/docker-compose.yml)
- [数据库 DDL](../db/schema.sql)
- [K线数据采集脚本](../scripts/download_kline.py)
- [策略筛选流水线](../docs/screening_pipeline.md)
