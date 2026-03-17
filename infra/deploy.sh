#!/usr/bin/env bash
# =============================================================================
#  infra/deploy.sh — MySQL Docker 一键部署脚本
#
#  功能：
#    1. 检查 Docker 环境
#    2. 拉取 mysql:latest 镜像
#    3. 启动 MySQL 容器（通过 docker-compose）
#    4. 等待容器健康检查通过
#    5. 可选：执行全量 K 线数据初始下载
#
#  用法：
#    bash infra/deploy.sh            # 只部署 MySQL（不下数据）
#    bash infra/deploy.sh --full     # 部署 + 首次全量下载（约 20 分钟）
#    bash infra/deploy.sh --update   # 部署 + 增量更新今日数据
# =============================================================================
set -euo pipefail

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 参数解析 ──────────────────────────────────────────────────────────────────
FULL_DOWNLOAD=false
UPDATE_ONLY=false
for arg in "$@"; do
  case $arg in
    --full)   FULL_DOWNLOAD=true ;;
    --update) UPDATE_ONLY=true ;;
    --help|-h)
      echo "用法: bash infra/deploy.sh [--full|--update]"
      echo "  (无参数)  只部署 MySQL Docker 容器"
      echo "  --full    部署 + 首次全量下载 K 线历史数据 (≈20 分钟)"
      echo "  --update  部署 + 增量更新今日 K 线数据 (≈3 分钟)"
      exit 0 ;;
  esac
done

# ── 定位项目根目录 ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  A股量化系统 — MySQL Docker 部署"
echo "  项目根目录: $PROJECT_ROOT"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── Step 1: 检查 Docker ───────────────────────────────────────────────────────
info "Step 1/5  检查 Docker 环境..."

if ! command -v docker &>/dev/null; then
  error "未找到 docker 命令，请先安装 Docker Desktop: https://docs.docker.com/get-docker/"
fi

if ! docker info &>/dev/null 2>&1; then
  error "Docker daemon 未运行，请启动 Docker Desktop 后重试"
fi

DOCKER_VERSION=$(docker --version | grep -oE '[0-9]+\.[0-9]+')
COMPOSE_VERSION=$(docker compose version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "N/A")
success "Docker $DOCKER_VERSION  |  Compose $COMPOSE_VERSION"

# ── Step 2: 检查 .env 配置 ────────────────────────────────────────────────────
info "Step 2/5  检查 .env 配置..."

if [[ ! -f ".env" ]]; then
  warn ".env 不存在，从模板创建..."
  cat > .env <<'EOF'
# MySQL 配置（Docker 容器）
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=quant
MYSQL_PASS=quant123
MYSQL_DB=quant
EOF
  success ".env 已创建（使用默认配置）"
else
  success ".env 已存在"
fi

# 读取配置
source .env 2>/dev/null || true
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_DB="${MYSQL_DB:-quant}"
MYSQL_USER="${MYSQL_USER:-quant}"
MYSQL_PASS="${MYSQL_PASS:-quant123}"

echo "  数据库: ${MYSQL_USER}@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DB}"

# ── Step 3: 拉取镜像 ──────────────────────────────────────────────────────────
info "Step 3/5  拉取 mysql:latest 镜像..."

CONTAINER_EXISTS=$(docker ps -a --filter "name=quant-mysql" --format "{{.Names}}" 2>/dev/null || echo "")

if [[ -n "$CONTAINER_EXISTS" ]]; then
  STATUS=$(docker inspect --format='{{.State.Status}}' quant-mysql 2>/dev/null || echo "unknown")
  if [[ "$STATUS" == "running" ]]; then
    warn "quant-mysql 容器已在运行，跳过拉取"
  else
    warn "quant-mysql 容器已存在（状态: $STATUS），重新启动..."
    docker compose -f docker/docker-compose.yml up -d mysql
  fi
else
  info "拉取 mysql:latest（首次可能需要几分钟）..."
  docker pull mysql:latest
  success "镜像拉取完成"
fi

# ── Step 4: 启动容器 ──────────────────────────────────────────────────────────
info "Step 4/5  启动 MySQL 容器..."

docker compose -f docker/docker-compose.yml up -d mysql

# 等待健康检查通过
info "等待 MySQL 健康检查（最多 60 秒）..."
MAX_WAIT=60
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
  HEALTH=$(docker inspect --format='{{.State.Health.Status}}' quant-mysql 2>/dev/null || echo "unknown")
  if [[ "$HEALTH" == "healthy" ]]; then
    success "MySQL 容器健康，准备就绪 ✓"
    break
  fi
  if [[ "$HEALTH" == "unhealthy" ]]; then
    error "MySQL 容器不健康，查看日志: docker logs quant-mysql"
  fi
  printf "  等待中... (%ds)  状态: %s\r" "$WAITED" "$HEALTH"
  sleep 3
  WAITED=$((WAITED + 3))
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
  warn "健康检查超时，继续尝试连接..."
fi

# ── Step 5: 验证连接 ──────────────────────────────────────────────────────────
info "Step 5/5  验证数据库连接..."

# 等待 2 秒让连接稳定
sleep 2

if docker exec quant-mysql mysqladmin ping -h localhost -u root "-p${MYSQL_PASS}" --silent &>/dev/null; then
  success "MySQL 连接验证通过"
else
  error "无法连接 MySQL，请检查: docker logs quant-mysql"
fi

# 显示表数量
TABLE_COUNT=$(docker exec quant-mysql mysql -u "${MYSQL_USER}" "-p${MYSQL_PASS}" "${MYSQL_DB}" \
  -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${MYSQL_DB}';" \
  --skip-column-names 2>/dev/null || echo "0")

KLINE_COUNT=$(docker exec quant-mysql mysql -u "${MYSQL_USER}" "-p${MYSQL_PASS}" "${MYSQL_DB}" \
  -e "SELECT COUNT(*) FROM kline_daily;" \
  --skip-column-names 2>/dev/null || echo "0")

echo ""
echo "═══════════════════════════════════════════════════════"
echo -e "  ${GREEN}MySQL 部署成功！${NC}"
echo "  容器名称:  quant-mysql"
echo "  连接地址:  ${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DB}"
echo "  用户名:    ${MYSQL_USER}"
echo "  数据表数:  ${TABLE_COUNT}"
echo "  K线记录:   ${KLINE_COUNT} 条"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 可选：数据下载 ────────────────────────────────────────────────────────────
if [[ "$FULL_DOWNLOAD" == "true" ]]; then
  echo ""
  info "开始全量 K 线数据下载（约 20 分钟，5000+ 只股票）..."
  python scripts/download_kline.py --days 500 --workers 3
  echo ""
  success "全量数据下载完成！"
  bash infra/status.sh

elif [[ "$UPDATE_ONLY" == "true" ]]; then
  echo ""
  info "开始增量数据更新（只下载今日新数据）..."
  python scripts/download_kline.py --update --workers 3
  echo ""
  success "增量更新完成！"
  bash infra/status.sh

else
  echo "提示：数据库已就绪，但尚无 K 线数据。"
  echo "  首次全量下载:  bash infra/deploy.sh --full"
  echo "  仅今日更新:    bash infra/update.sh"
fi

echo ""
echo "常用命令："
echo "  查看状态:    bash infra/status.sh"
echo "  每日更新:    bash infra/update.sh"
echo "  备份数据:    bash infra/backup.sh"
echo "  停止容器:    docker compose -f docker/docker-compose.yml stop mysql"
echo ""
