#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行 HTTPS 切换脚本" >&2
  exit 1
fi

config=/etc/nginx/conf.d/ozon-erp.conf
if [ ! -f "$config" ]; then
  echo "找不到 ERP 的 Nginx 配置，请先运行安装器" >&2
  exit 1
fi

cp -p "$config" "$config.pre-standard-https"
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=https >/dev/null
  firewall-cmd --reload >/dev/null
fi
systemctl enable nginx
systemctl start nginx
systemctl enable ozon-erp
systemctl restart ozon-erp

bash "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/configure-nginx.sh"

echo "ERP 已切换到标准 HTTPS 端口"
echo "网址：https://8.217.125.205/"
echo "请使用 ERP 应用登录页登录或注册"
