#!/usr/bin/env bash
# 拾光 SeeGlow · VPS 一键部署脚本（Docker + Caddy 自动 HTTPS）
#
# 前置：一台 Ubuntu/Debian VPS + 一个已解析到该 VPS 的域名（A 记录）
# 用法：把 开源发布版/ 整个文件夹上传到服务器后：
#   sudo bash deploy.sh your-domain.com
#
# 密钥：脚本自动读取同目录 .env.deploy（不含请先生成，见 部署-服务器与域名.md）
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "用法: sudo bash deploy.sh your-domain.com"; exit 1
fi
if [ "$(id -u)" -ne 0 ]; then echo "请用 sudo 运行"; exit 1; fi

cd "$(dirname "$0")"

# ---------- 密钥 ----------
ENV_FILE=".env.deploy"
if [ ! -f "$ENV_FILE" ]; then
  echo "==> 未找到 $ENV_FILE，生成模板（请编辑填入你的 API Key 等）"
  SECRET_UUID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
  cat > "$ENV_FILE" <<EOF
SEELOW_AUTH_SECRET=${SECRET_UUID//-/}${SECRET_UUID//-/}
SEELOW_API_BASE=https://api.siliconflow.cn/v1
SEELOW_API_KEY=sk-把这里换成你的硅基流动Key
AFDIAN_USER_ID=你的爱发电开发者ID
AFDIAN_TOKEN=你的爱发电开发者Token
EOF
  chmod 600 "$ENV_FILE"
  echo "    已生成 $ENV_FILE —— 请填入真实值后重新运行本脚本"; exit 1
fi
set -a; source "$ENV_FILE"; set +a
if [[ "${SEELOW_API_KEY:-}" == sk-把这里换成* ]]; then
  echo "!! 请先编辑 $ENV_FILE 填入真实 API Key"; exit 1
fi

echo "==> 1/4 安装 Docker 与 Caddy"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 caddy curl >/dev/null 2>&1 || {
  apt-get install -y -qq docker.io docker-compose caddy curl; }
systemctl enable --now docker

echo "==> 2/4 构建镜像"
docker build -t seeglow:latest .

echo "==> 3/4 启动容器（数据持久化在 /data/seeglow）"
mkdir -p /data/seeglow
docker rm -f seeglow 2>/dev/null || true
docker run -d --name seeglow --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -e SEELOW_PUBLIC=1 -e SEELOW_SITE_PAID=1 \
  -e SEELOW_OUTPUT_DIR=/data/shiguang \
  -e SEELOW_AUTH_SECRET="$SEELOW_AUTH_SECRET" \
  -e SEELOW_API_BASE="$SEELOW_API_BASE" \
  -e SEELOW_API_KEY="$SEELOW_API_KEY" \
  -e AFDIAN_USER_ID="$AFDIAN_USER_ID" \
  -e AFDIAN_TOKEN="$AFDIAN_TOKEN" \
  -v /data/seeglow:/data \
  seeglow:latest

echo "==> 4/4 配置 Caddy（自动申请 HTTPS 证书）"
cat >/etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8765
}
EOF
systemctl restart caddy

echo ""
echo "✅ 部署完成：https://${DOMAIN}"
echo "   官网首页 = /    应用 = /app    日志 = docker logs -f seeglow"
echo "   更新 = 重新上传代码后重跑本脚本（用户数据在 /data/seeglow 不受影响）"
