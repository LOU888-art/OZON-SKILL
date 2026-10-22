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

    auth_basic "OZON ERP";
    auth_basic_user_file /etc/nginx/ozon-erp/htpasswd;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-ERP-User $remote_user;
        proxy_read_timeout 300s;
    }
}
EOF

nginx -t
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=https >/dev/null
  firewall-cmd --reload >/dev/null
fi
systemctl enable nginx
systemctl restart nginx
systemctl enable ozon-erp
systemctl restart ozon-erp

status=$(curl -k -sS -o /dev/null -w '%{http_code}' -H 'Host: 8.217.125.205' https://127.0.0.1/)
if [ "$status" != "401" ]; then
  echo "HTTPS 本机检查失败，预期 401，实际 $status" >&2
  exit 1
fi

echo "ERP 已切换到标准 HTTPS 端口"
echo "网址：https://8.217.125.205/"
echo "用户名和密码保持不变"
