#!/usr/bin/env bash
# =============================================================================
#  infra/restore.sh — MySQL 数据恢复
#
#  功能：
#    1. 从 .sql 或 .sql.gz 备份文件恢复数据
#    2. 恢复前自动备份当前数据（可跳过）
#    3. 显示恢复后统计信息
#
#  用法：
#    bash infra/restore.sh backups/quant_20260317_153000.sql.gz
#    bash infra/restore.sh backups/quant_20260317_153000.sql.gz --no-pre-backup
#    bash infra/restore.sh --latest     # 自动恢复最新备份
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

[[ -f ".env" ]] && source .env 2>/dev/null || true
MYSQL_DB="${MYSQL_DB:-quant}"
MYSQL_USER="${MYSQL_USER:-quant}"
MYSQL_PASS="${MYSQL_PASS:-quant123}"

# ── 参数解析 ──────────────────────────────────────────────────────────────────
BACKUP_FILE=""
PRE_BACKUP=true
USE_LATEST=false

for arg in "$@"; do
  case $arg in
    --latest)          USE_LATEST=true ;;
    --no-pre-backup)   PRE_BACKUP=false ;;
    --help|-h)
      echo "用法: bash infra/restore.sh <备份文件> [选项]"
      echo "  <备份文件>         .sql 或 .sql.gz 备份文件路径"
      echo "  --latest           自动选择最新备份文件"
      echo "  --no-pre-backup    跳过恢复前自动备份当前数据"
      exit 0 ;;
    *)
      if [[ -z "$BACKUP_FILE" ]]; then
        BACKUP_FILE="$arg"
      fi ;;
  esac
done

# 自动选择最新备份
if $USE_LATEST; then
  BACKUP_FILE=$(ls -t "$PROJECT_ROOT/backups"/quant_*.sql* 2>/dev/null | head -1 || echo "")
  [[ -z "$BACKUP_FILE" ]] && error "backups/ 目录中没有备份文件"
  info "自动选择最新备份: $BACKUP_FILE"
fi

[[ -z "$BACKUP_FILE" ]] && error "请指定备份文件: bash infra/restore.sh <备份文件>"
[[ ! -f "$BACKUP_FILE" ]] && error "备份文件不存在: $BACKUP_FILE"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  A股量化系统 — 数据库恢复"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 检查容器 ──────────────────────────────────────────────────────────────────
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' quant-mysql 2>/dev/null || echo "not_found")
[[ "$CONTAINER_STATUS" != "running" ]] && error "quant-mysql 未运行，请先启动: bash infra/deploy.sh"

# ── 显示备份文件信息 ──────────────────────────────────────────────────────────
FILE_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
info "备份文件: $BACKUP_FILE ($FILE_SIZE)"

# ── 恢复前先备份当前数据 ──────────────────────────────────────────────────────
if $PRE_BACKUP; then
  CURRENT_COUNT=$(docker exec quant-mysql mysql -u "$MYSQL_USER" "-p${MYSQL_PASS}" "$MYSQL_DB" \
    --skip-column-names -s -e "SELECT COUNT(*) FROM kline_daily;" 2>/dev/null || echo "0")

  if [[ "$CURRENT_COUNT" -gt 0 ]]; then
    info "恢复前自动备份当前数据（$CURRENT_COUNT 条K线）..."
    bash "$SCRIPT_DIR/backup.sh" --keep 10
    success "当前数据已备份"
  fi
fi

# ── 二次确认 ─────────────────────────────────────────────────────────────────
warn "即将恢复数据库 $MYSQL_DB，当前数据将被覆盖！"
read -r -p "  确认继续？[y/N] " CONFIRM
if [[ "$CONFIRM" != "y" ]] && [[ "$CONFIRM" != "Y" ]]; then
  info "已取消"
  exit 0
fi

# ── 执行恢复 ──────────────────────────────────────────────────────────────────
T0=$(date +%s)
info "开始恢复..."

# 确保目标库存在
docker exec quant-mysql mysql -u root "-p${MYSQL_PASS}" \
  -e "CREATE DATABASE IF NOT EXISTS ${MYSQL_DB} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null

# 根据文件类型选择解压方式
if [[ "$BACKUP_FILE" == *.gz ]]; then
  zcat "$BACKUP_FILE" | docker exec -i quant-mysql mysql -u root "-p${MYSQL_PASS}" "$MYSQL_DB" 2>/dev/null
else
  docker exec -i quant-mysql mysql -u root "-p${MYSQL_PASS}" "$MYSQL_DB" < "$BACKUP_FILE" 2>/dev/null
fi

T1=$(date +%s)
ELAPSED=$((T1 - T0))

# ── 验证恢复结果 ──────────────────────────────────────────────────────────────
AFTER_COUNT=$(docker exec quant-mysql mysql -u "$MYSQL_USER" "-p${MYSQL_PASS}" "$MYSQL_DB" \
  --skip-column-names -s -e "SELECT COUNT(*) FROM kline_daily;" 2>/dev/null || echo "0")
AFTER_LATEST=$(docker exec quant-mysql mysql -u "$MYSQL_USER" "-p${MYSQL_PASS}" "$MYSQL_DB" \
  --skip-column-names -s -e "SELECT MAX(trade_date) FROM kline_daily;" 2>/dev/null || echo "N/A")

echo ""
echo "═══════════════════════════════════════════════════════"
echo -e "  ${GREEN}恢复成功！${NC}"
printf "  耗时:   %dm%ds\n" $((ELAPSED/60)) $((ELAPSED%60))
echo "  K线:    $AFTER_COUNT 条（最新: $AFTER_LATEST）"
echo "  来源:   $BACKUP_FILE"
echo "═══════════════════════════════════════════════════════"
echo ""
