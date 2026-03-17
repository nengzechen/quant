#!/usr/bin/env bash
# =============================================================================
#  infra/status.sh — MySQL 数据库状态检查
#
#  显示内容：
#    - Docker 容器运行状态
#    - MySQL 连接状态
#    - 各表数据量统计
#    - K 线数据覆盖情况（最新/最旧日期）
#    - 数据缺口检测（哪些股票缺少今日数据）
#    - 磁盘占用
#
#  用法：
#    bash infra/status.sh             # 完整状态报告
#    bash infra/status.sh --quick     # 仅容器 + 行数（快速版）
#    bash infra/status.sh --gaps      # 只显示数据缺口
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[✓]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[!]${NC}    $*"; }
fail()    { echo -e "${RED}[✗]${NC}    $*"; }
header()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; echo "$(printf '─%.0s' {1..55})"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 参数
QUICK=false; GAPS_ONLY=false
for arg in "$@"; do
  case $arg in
    --quick) QUICK=true ;;
    --gaps)  GAPS_ONLY=true ;;
  esac
done

# 读取 .env
[[ -f ".env" ]] && source .env 2>/dev/null || true
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_DB="${MYSQL_DB:-quant}"
MYSQL_USER="${MYSQL_USER:-quant}"
MYSQL_PASS="${MYSQL_PASS:-quant123}"

# MySQL 快捷查询函数
mysql_q() {
  docker exec quant-mysql mysql -u "${MYSQL_USER}" "-p${MYSQL_PASS}" "${MYSQL_DB}" \
    --skip-column-names -s -e "$1" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  A股量化系统 — MySQL 数据库状态报告${NC}"
echo -e "  $(date '+%Y-%m-%d %H:%M:%S')"
echo -e "${BOLD}════════════════════════════════════════════════════════${NC}"

# ═══ 1. Docker 容器状态 ═══════════════════════════════════════
header "1. Docker 容器"

CONTAINER_EXISTS=$(docker ps -a --filter "name=quant-mysql" --format "{{.Names}}" 2>/dev/null || echo "")

if [[ -z "$CONTAINER_EXISTS" ]]; then
  fail "quant-mysql 容器不存在"
  echo "  运行 'bash infra/deploy.sh' 来部署"
  exit 1
fi

STATUS=$(docker inspect --format='{{.State.Status}}' quant-mysql 2>/dev/null)
HEALTH=$(docker inspect --format='{{.State.Health.Status}}' quant-mysql 2>/dev/null || echo "N/A")
IMAGE=$(docker inspect --format='{{.Config.Image}}' quant-mysql 2>/dev/null)
STARTED=$(docker inspect --format='{{.State.StartedAt}}' quant-mysql 2>/dev/null | cut -c1-19)
UPTIME=$(docker ps --filter "name=quant-mysql" --format "{{.Status}}" 2>/dev/null || echo "N/A")

if [[ "$STATUS" == "running" ]]; then
  success "容器状态: running"
else
  fail "容器状态: $STATUS （请运行 docker compose -f docker/docker-compose.yml up -d mysql）"
fi

if [[ "$HEALTH" == "healthy" ]]; then
  success "健康检查: healthy ✓"
elif [[ "$HEALTH" == "N/A" ]]; then
  warn "健康检查: 无 healthcheck 配置"
else
  warn "健康检查: $HEALTH"
fi

echo "  镜像:     $IMAGE"
echo "  启动时间: $STARTED"
echo "  运行时长: $UPTIME"

# ═══ 2. MySQL 连接验证 ════════════════════════════════════════
header "2. MySQL 连接"

if docker exec quant-mysql mysqladmin ping -h localhost -u root "-p${MYSQL_PASS}" --silent &>/dev/null; then
  success "MySQL 连接正常"
else
  fail "无法连接 MySQL"
  echo "  查看日志: docker logs quant-mysql --tail 50"
  exit 1
fi

VERSION=$(docker exec quant-mysql mysql -u root "-p${MYSQL_PASS}" -e "SELECT VERSION();" --skip-column-names -s 2>/dev/null)
echo "  版本:     MySQL $VERSION"
echo "  地址:     ${MYSQL_USER}@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DB}"

if $QUICK && ! $GAPS_ONLY; then
  echo ""
fi

# ═══ 3. 数据统计 ═════════════════════════════════════════════
header "3. 数据统计"

# stock_info
STOCK_TOTAL=$(mysql_q "SELECT COUNT(*) FROM stock_info;" || echo "0")
STOCK_ACTIVE=$(mysql_q "SELECT COUNT(*) FROM stock_info WHERE is_active=1 AND is_st=0;" || echo "0")
echo "  stock_info（股票基础信息）:"
echo "    总数:   $STOCK_TOTAL 只"
echo "    可交易: $STOCK_ACTIVE 只（非ST、非北交所）"

# kline_daily
KLINE_TOTAL=$(mysql_q "SELECT COUNT(*) FROM kline_daily;" || echo "0")
KLINE_MIN=$(mysql_q "SELECT MIN(trade_date) FROM kline_daily;" || echo "N/A")
KLINE_MAX=$(mysql_q "SELECT MAX(trade_date) FROM kline_daily;" || echo "N/A")
KLINE_STOCKS=$(mysql_q "SELECT COUNT(DISTINCT code) FROM kline_daily;" || echo "0")
KLINE_DAYS=$(mysql_q "SELECT COUNT(DISTINCT trade_date) FROM kline_daily;" || echo "0")

echo "  kline_daily（日线数据）:"
echo "    总行数:   $KLINE_TOTAL 条"
echo "    股票数:   $KLINE_STOCKS 只"
echo "    交易日数: $KLINE_DAYS 天"
echo "    数据范围: $KLINE_MIN  ~  $KLINE_MAX"

# 今日/最新日数据
LATEST_DATE=$(mysql_q "SELECT MAX(trade_date) FROM kline_daily;" || echo "N/A")
LATEST_COUNT=$(mysql_q "SELECT COUNT(*) FROM kline_daily WHERE trade_date='${LATEST_DATE}';" || echo "0")
TODAY=$(date +%Y-%m-%d)

if [[ "$LATEST_DATE" == "$TODAY" ]]; then
  success "最新数据: $LATEST_DATE（今日，$LATEST_COUNT 只）"
elif [[ "$LATEST_DATE" > "$(date -d '3 days ago' +%Y-%m-%d 2>/dev/null || date -v-3d +%Y-%m-%d 2>/dev/null || echo '1970-01-01')" ]]; then
  warn "最新数据: $LATEST_DATE（$LATEST_COUNT 只，今日尚未更新）"
else
  fail "最新数据: $LATEST_DATE（数据较旧，请运行 bash infra/update.sh）"
fi

# download_progress
PROG_TOTAL=$(mysql_q "SELECT COUNT(*) FROM download_progress;" || echo "0")
PROG_ERROR=$(mysql_q "SELECT COUNT(*) FROM download_progress WHERE status='error';" || echo "0")
echo "  download_progress（下载进度）:"
echo "    已记录: $PROG_TOTAL 只"
if [[ "$PROG_ERROR" -gt 0 ]]; then
  warn "    失败:   $PROG_ERROR 只（详见 logs/download_kline.log）"
else
  success "    失败:   0 只"
fi

if $QUICK && ! $GAPS_ONLY; then
  echo ""
  echo -e "${BOLD}════════════════════════════════════════════════════════${NC}"
  exit 0
fi

# ═══ 4. 磁盘占用 ══════════════════════════════════════════════
header "4. 磁盘占用"

DB_SIZE=$(mysql_q "
  SELECT CONCAT(ROUND(SUM(data_length + index_length) / 1024 / 1024, 1), ' MB')
  FROM information_schema.tables
  WHERE table_schema = '${MYSQL_DB}';" || echo "N/A")

VOLUME_SIZE=$(docker system df -v 2>/dev/null | grep "quant_mysql_data" | awk '{print $4}' || echo "N/A")

echo "  数据库大小:    $DB_SIZE"
echo "  Docker 卷大小: ${VOLUME_SIZE:-N/A}"

# 各表大小
echo "  各表详细大小:"
mysql_q "
  SELECT
    table_name,
    CONCAT(ROUND((data_length + index_length) / 1024 / 1024, 1), ' MB') AS size,
    table_rows AS approx_rows
  FROM information_schema.tables
  WHERE table_schema = '${MYSQL_DB}'
  ORDER BY (data_length + index_length) DESC;" 2>/dev/null | \
while IFS=$'\t' read -r tname size rows; do
  printf "    %-25s %10s  (~%s 行)\n" "$tname" "$size" "$rows"
done

# ═══ 5. 数据缺口检测 ══════════════════════════════════════════
if $GAPS_ONLY || ! $QUICK; then
  header "5. 数据缺口检测"

  ACTIVE_CODES=$(mysql_q "SELECT COUNT(*) FROM stock_info WHERE is_active=1 AND is_st=0;" || echo "0")
  LATEST_CODES=$(mysql_q "SELECT COUNT(DISTINCT code) FROM kline_daily WHERE trade_date='${LATEST_DATE}';" || echo "0")
  MISSING=$((ACTIVE_CODES - LATEST_CODES))

  echo "  可交易股票:       $ACTIVE_CODES 只"
  echo "  最新日有数据:     $LATEST_CODES 只"
  if [[ "$MISSING" -le 0 ]]; then
    success "  数据缺口:         无 ✓（数据完整）"
  elif [[ "$MISSING" -le 50 ]]; then
    warn "  数据缺口:         $MISSING 只（北交所/新股/停牌，正常）"
  else
    fail "  数据缺口:         $MISSING 只（需要运行 bash infra/update.sh）"
  fi

  # 显示下载失败列表（最多 10 条）
  ERRORS=$(mysql_q "
    SELECT code, error_msg FROM download_progress
    WHERE status='error' LIMIT 10;" 2>/dev/null || echo "")

  if [[ -n "$ERRORS" ]]; then
    echo "  下载失败股票（最多显示10条）:"
    echo "$ERRORS" | while IFS=$'\t' read -r code msg; do
      printf "    %s  %s\n" "$code" "${msg:0:60}"
    done
  fi
fi

# ═══ 6. 最新交易日 Top10（验证数据质量） ══════════════════════
if ! $QUICK && ! $GAPS_ONLY; then
  header "6. 最新交易日 数据样本（前5条）"

  mysql_q "
    SELECT code, close, pct_chg, turnover, ma5, ma20
    FROM kline_daily
    WHERE trade_date = '${LATEST_DATE}'
    ORDER BY pct_chg DESC LIMIT 5;" 2>/dev/null | \
  while IFS=$'\t' read -r code close pchg turn ma5 ma20; do
    printf "  %s  收盘:%-8s  涨跌:%-7s  换手:%-6s  MA5:%-8s  MA20:%-8s\n" \
      "$code" "$close" "$pchg" "$turn" "$ma5" "$ma20"
  done
fi

echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════${NC}"
echo "  快速操作："
echo "    更新今日数据:  bash infra/update.sh"
echo "    备份数据库:    bash infra/backup.sh"
echo "    查看容器日志:  docker logs quant-mysql --tail 100"
echo -e "${BOLD}════════════════════════════════════════════════════════${NC}"
echo ""
