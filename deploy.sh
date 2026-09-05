#!/usr/bin/env bash
# 拾光 SeeGlow · VPS 一键部署脚本（Docker + Caddy 自动 HTTPS）
#
# 前置：一台 Ubuntu/Debian VPS + 一个已解析到该 VPS 的域名
# 用法：把 开源发布版/ 上传到服务器后，在服务器上执行
#   sudo bash deploy.sh your-domain.com
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "用法: sudo bash deploy.sh your-domain.com"; exit 1
fi
if [ "$(id -u)" -ne 0 ]; then echo "请用 sudo 运行"; exit 1; fi

echo "==> 1/4 安装 Docker 与 Caddy"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 caddy curl >/dev/null 2>&1 || {
  apt-get install -y -qq docker.io docker-compose caddy curl; }
systemctl enable --now docker

echo "==> 2/4 构建镜像"
docker build -t seeglow:latest .

echo "==> 3/4 启动容器"
mkdir -p /data/seeglow
docker rm -f seeglow 2>/dev/null || true
docker run -d --name seeglow --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -e SEELOW_PUBLIC=1 -e SEELOW_SITE_PAID=1 \
  -e SEELOW_OUTPUT_DIR=/data/shiguang \
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
echo "   容器: docker logs -f seeglow   |   更新: 重新上传代码后重跑本脚本"
echo "   记得在宿主机创建 Modal 所需的 secrets（见 部署-手机访问.md），"
echo "   或用 docker run -e 注入 SEELOW_AUTH_SECRET / AFDIAN_* / SEELOW_API_KEY。"
