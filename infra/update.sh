#!/usr/bin/env bash
# =============================================================================
#  infra/update.sh — 每日 K 线数据增量更新
#
#  功能：
#    1. 检查 MySQL 容器是否运行，必要时自动启动
#    2. 调用 scripts/download_kline.py --update 增量更新今日数据
#    3. 更新完毕后自动打印状态摘要
#    4. 可配置为 crontab 定时任务（每日 15:30 自动运行）
#
#  用法：
#    bash infra/update.sh               # 增量更新今日数据
#    bash infra/update.sh --full        # 全量重新下载（500个交易日历史）
#    bash infra/update.sh --codes 600519,000001   # 只更新指定股票
#    bash infra/update.sh --workers 5   # 指定并发数（默认 3）
#
#  定时任务（每日收盘后 15:35 自动运行）：
#    crontab -e
#    35 15 * * 1-5 cd /path/to/quant && bash infra/update.sh >> logs/cron_update.log 2>&1
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── 参数解析 ──────────────────────────────────────────────────────────────────
FULL=false
CODES=""
WORKERS=3
for arg in "$@"; do
  case $arg in
    --full)           FULL=true ;;
    --codes)          shift; CODES="$1" ;;
    --codes=*)        CODES="${arg#*=}" ;;
    --workers)        shift; WORKERS="$1" ;;
    --workers=*)      WORKERS="${arg#*=}" ;;
    --help|-h)
      echo "用法: bash infra/update.sh [选项]"
      echo "  (无参数)          增量更新今日K线数据"
      echo "  --full            全量重新下载（近500交易日历史）"
      echo "  --codes CODE,...  只更新指定股票，逗号分隔"
      echo "  --workers N       并发线程数（默认3）"
      exit 0 ;;
  esac
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  A股量化系统 — K线数据更新"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 读取 .env ─────────────────────────────────────────────────────────────────
[[ -f ".env" ]] && source .env 2>/dev/null || true
MYSQL_PASS="${MYSQL_PASS:-quant123}"

# ── 1. 检查 Docker 容器 ───────────────────────────────────────────────────────
info "检查 MySQL 容器状态..."

CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' quant-mysql 2>/dev/null || echo "not_found")

case "$CONTAINER_STATUS" in
  running)
    success "quant-mysql 正在运行"
    ;;
  exited|stopped)
    warn "quant-mysql 已停止，正在重启..."
    docker compose -f docker/docker-compose.yml up -d mysql
    sleep 5
    ;;
  not_found)
    error "quant-mysql 容器不存在，请先运行: bash infra/deploy.sh"
    ;;
  *)
    warn "容器状态: $CONTAINER_STATUS，尝试启动..."
    docker compose -f docker/docker-compose.yml up -d mysql
    sleep 5
    ;;
esac

# 等待健康检查
for i in {1..10}; do
  HEALTH=$(docker inspect --format='{{.State.Health.Status}}' quant-mysql 2>/dev/null || echo "unknown")
  if [[ "$HEALTH" == "healthy" ]]; then
    success "MySQL 健康检查通过"
    break
  fi
  if [[ $i -eq 10 ]]; then
    warn "健康检查超时，继续执行..."
  fi
  sleep 3
done

# ── 2. 查看更新前数据状态 ─────────────────────────────────────────────────────
BEFORE_COUNT=$(docker exec quant-mysql mysql -u "${MYSQL_USER:-quant}" "-p${MYSQL_PASS}" \
  "${MYSQL_DB:-quant}" --skip-column-names -s \
  -e "SELECT COUNT(*) FROM kline_daily;" 2>/dev/null || echo "0")

BEFORE_LATEST=$(docker exec quant-mysql mysql -u "${MYSQL_USER:-quant}" "-p${MYSQL_PASS}" \
  "${MYSQL_DB:-quant}" --skip-column-names -s \
  -e "SELECT MAX(trade_date) FROM kline_daily;" 2>/dev/null || echo "N/A")

info "更新前: $BEFORE_COUNT 条记录，最新日期: $BEFORE_LATEST"

# ── 3. 执行数据更新 ───────────────────────────────────────────────────────────
echo ""
UPDATE_START=$(date +%s)

# 构建命令参数
CMD_ARGS="--workers $WORKERS"

if [[ "$FULL" == "true" ]]; then
  info "模式: 全量下载（近500个交易日历史数据）"
  CMD_ARGS="--days 500 $CMD_ARGS"
else
  info "模式: 增量更新（只补充最新数据）"
  CMD_ARGS="--update $CMD_ARGS"
fi

if [[ -n "$CODES" ]]; then
  info "范围: 指定股票 $CODES"
  CMD_ARGS="$CMD_ARGS --codes $CODES"
fi

python scripts/download_kline.py $CMD_ARGS

UPDATE_END=$(date +%s)
ELAPSED=$((UPDATE_END - UPDATE_START))

# ── 4. 查看更新后状态 ─────────────────────────────────────────────────────────
echo ""
AFTER_COUNT=$(docker exec quant-mysql mysql -u "${MYSQL_USER:-quant}" "-p${MYSQL_PASS}" \
  "${MYSQL_DB:-quant}" --skip-column-names -s \
  -e "SELECT COUNT(*) FROM kline_daily;" 2>/dev/null || echo "0")

AFTER_LATEST=$(docker exec quant-mysql mysql -u "${MYSQL_USER:-quant}" "-p${MYSQL_PASS}" \
  "${MYSQL_DB:-quant}" --skip-column-names -s \
  -e "SELECT MAX(trade_date) FROM kline_daily;" 2>/dev/null || echo "N/A")

NEW_ROWS=$((AFTER_COUNT - BEFORE_COUNT))

echo "═══════════════════════════════════════════════════════"
echo -e "  ${GREEN}更新完成！${NC}"
printf "  耗时:     %dm%ds\n" $((ELAPSED/60)) $((ELAPSED%60))
echo "  新增行数: +$NEW_ROWS 条"
echo "  总记录:   $AFTER_COUNT 条"
echo "  最新日期: $AFTER_LATEST"
echo "═══════════════════════════════════════════════════════"
echo ""

# 今日是交易日但数据未更新时提醒
TODAY=$(date +%Y-%m-%d)
WEEKDAY=$(date +%u)   # 1=Mon ... 7=Sun

if [[ "$WEEKDAY" -le 5 ]] && [[ "$AFTER_LATEST" != "$TODAY" ]]; then
  CURRENT_HOUR=$(date +%H)
  if [[ "$CURRENT_HOUR" -ge 16 ]]; then
    warn "今日（$TODAY）数据仍未更新，可能是节假日或数据源延迟"
  else
    info "今日（$TODAY）收盘后（15:30之后）请再次运行以获取最新数据"
  fi
fi
