#!/usr/bin/env bash
# =============================================================================
#  infra/backup.sh — MySQL 数据备份
#
#  功能：
#    1. 使用 mysqldump 导出完整数据库
#    2. gzip 压缩备份文件
#    3. 自动按日期命名，保留最近 N 份
#    4. 显示备份大小和耗时
#
#  用法：
#    bash infra/backup.sh               # 备份到 backups/ 目录
#    bash infra/backup.sh --dir /path   # 备份到指定目录
#    bash infra/backup.sh --keep 7      # 保留最近 7 份（默认 5）
#    bash infra/backup.sh --no-compress # 不压缩（.sql 格式）
#
#  备份文件命名：
#    backups/quant_YYYYMMDD_HHMMSS.sql.gz
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
BACKUP_DIR="$PROJECT_ROOT/backups"
KEEP=5
COMPRESS=true
for arg in "$@"; do
  case $arg in
    --dir)         shift; BACKUP_DIR="$1" ;;
    --dir=*)       BACKUP_DIR="${arg#*=}" ;;
    --keep)        shift; KEEP="$1" ;;
    --keep=*)      KEEP="${arg#*=}" ;;
    --no-compress) COMPRESS=false ;;
    --help|-h)
      echo "用法: bash infra/backup.sh [选项]"
      echo "  --dir DIR      备份输出目录（默认: backups/）"
      echo "  --keep N       保留最近N份备份（默认: 5）"
      echo "  --no-compress  不压缩，输出 .sql 文件"
      exit 0 ;;
  esac
done

[[ -f ".env" ]] && source .env 2>/dev/null || true
MYSQL_DB="${MYSQL_DB:-quant}"
MYSQL_USER="${MYSQL_USER:-quant}"
MYSQL_PASS="${MYSQL_PASS:-quant123}"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
mkdir -p "$BACKUP_DIR"

if $COMPRESS; then
  BACKUP_FILE="$BACKUP_DIR/quant_${TIMESTAMP}.sql.gz"
else
  BACKUP_FILE="$BACKUP_DIR/quant_${TIMESTAMP}.sql"
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  A股量化系统 — 数据库备份"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 检查容器状态 ──────────────────────────────────────────────────────────────
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' quant-mysql 2>/dev/null || echo "not_found")
[[ "$CONTAINER_STATUS" != "running" ]] && error "quant-mysql 未运行，请先启动: bash infra/deploy.sh"

# ── 获取数据统计（备份前记录） ────────────────────────────────────────────────
KLINE_COUNT=$(docker exec quant-mysql mysql -u "$MYSQL_USER" "-p${MYSQL_PASS}" "$MYSQL_DB" \
  --skip-column-names -s -e "SELECT COUNT(*) FROM kline_daily;" 2>/dev/null || echo "0")
LATEST_DATE=$(docker exec quant-mysql mysql -u "$MYSQL_USER" "-p${MYSQL_PASS}" "$MYSQL_DB" \
  --skip-column-names -s -e "SELECT MAX(trade_date) FROM kline_daily;" 2>/dev/null || echo "N/A")

info "备份数据库: $MYSQL_DB（$KLINE_COUNT 条K线，最新: $LATEST_DATE）"
info "备份文件:   $BACKUP_FILE"

# ── 执行备份 ──────────────────────────────────────────────────────────────────
T0=$(date +%s)

if $COMPRESS; then
  docker exec quant-mysql mysqldump \
    -u "$MYSQL_USER" "-p${MYSQL_PASS}" \
    --single-transaction \
    --routines \
    --triggers \
    --no-tablespaces \
    "$MYSQL_DB" 2>/dev/null | gzip > "$BACKUP_FILE"
else
  docker exec quant-mysql mysqldump \
    -u "$MYSQL_USER" "-p${MYSQL_PASS}" \
    --single-transaction \
    --routines \
    --triggers \
    --no-tablespaces \
    "$MYSQL_DB" 2>/dev/null > "$BACKUP_FILE"
fi

T1=$(date +%s)
ELAPSED=$((T1 - T0))

# 验证备份文件
if [[ ! -f "$BACKUP_FILE" ]] || [[ ! -s "$BACKUP_FILE" ]]; then
  error "备份文件为空或不存在: $BACKUP_FILE"
fi

FILE_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)

echo ""
echo "═══════════════════════════════════════════════════════"
echo -e "  ${GREEN}备份成功！${NC}"
echo "  文件:   $BACKUP_FILE"
echo "  大小:   $FILE_SIZE"
printf "  耗时:   %dm%ds\n" $((ELAPSED/60)) $((ELAPSED%60))
echo "  K线:    $KLINE_COUNT 条（最新: $LATEST_DATE）"
echo "═══════════════════════════════════════════════════════"

# ── 保留最近 N 份，清理旧备份 ────────────────────────────────────────────────
EXISTING_COUNT=$(ls "$BACKUP_DIR"/quant_*.sql* 2>/dev/null | wc -l | tr -d ' ')
if [[ "$EXISTING_COUNT" -gt "$KEEP" ]]; then
  TO_DELETE=$((EXISTING_COUNT - KEEP))
  info "清理旧备份（保留 $KEEP 份，删除 $TO_DELETE 份）..."
  ls -t "$BACKUP_DIR"/quant_*.sql* 2>/dev/null | tail -n "$TO_DELETE" | xargs rm -f
  success "旧备份已清理"
fi

echo ""
echo "当前备份列表："
ls -lh "$BACKUP_DIR"/quant_*.sql* 2>/dev/null | awk '{print "  " $NF " (" $5 ")"}'
echo ""
echo "恢复命令: bash infra/restore.sh $BACKUP_FILE"
echo ""
