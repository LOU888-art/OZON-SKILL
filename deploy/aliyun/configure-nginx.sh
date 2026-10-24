#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 配置 Nginx" >&2
  exit 1
fi

config=/etc/nginx/conf.d/ozon-erp.conf

cat >"$config" <<'EOF'
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    listen 8443 ssl;
    listen [::]:8443 ssl;
    server_name 8.217.125.205;

    ssl_certificate /etc/nginx/ozon-erp/server.crt;
    ssl_certificate_key /etc/nginx/ozon-erp/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Explicitly override any inherited legacy Basic Auth. Authentication is
    # handled by the ERP login/registration page, never by a browser popup.
    auth_basic off;

    location / {
        auth_basic off;
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-ERP-User "";
        proxy_read_timeout 300s;
    }
}
EOF

nginx -t
systemctl reload nginx

headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
status=$(curl -k -sS -D "$headers" -o /dev/null -w '%{http_code}' \
  -H 'Host: 8.217.125.205' https://127.0.0.1/)
if [ "$status" != "303" ]; then
  echo "HTTPS 入口检查失败：未登录访问应跳转到应用登录页（303），实际 HTTP $status" >&2
  exit 1
fi
if grep -qi '^WWW-Authenticate:' "$headers"; then
  echo "HTTPS 入口仍返回浏览器身份验证头，已停止部署" >&2
  exit 1
fi
location=$(sed -n 's/^Location:[[:space:]]*\([^[:space:]\r]*\).*/\1/p' "$headers" | tail -n 1)
if [ "$location" != "/login" ]; then
  echo "HTTPS 入口未跳转至 /login，实际 Location: ${location:-缺失}" >&2
  exit 1
fi

echo "Nginx 已关闭浏览器身份验证，应用登录/注册页检查通过。"
