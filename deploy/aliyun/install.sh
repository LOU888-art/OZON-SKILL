#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行安装器" >&2
  exit 1
fi

secret_file=${1:-/root/秘钥.txt}
if [ ! -r "$secret_file" ]; then
  echo "找不到密钥文件：$secret_file" >&2
  exit 1
fi
chmod 600 "$secret_file"

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
install_root=/opt/ozon-erp
app_dir=$install_root/app
venv_dir=$install_root/venv
state_dir=/var/lib/ozon-erp
config_dir=/etc/ozon-erp
nginx_secret_dir=/etc/nginx/ozon-erp

access_key=$(sed -n 's/^[[:space:]]*Access Key[[:space:]]*[:：][[:space:]]*//p' "$secret_file" | tr -d '\r\n')
secret_key=$(sed -n 's/^[[:space:]]*Secret Key[[:space:]]*[:：][[:space:]]*//p' "$secret_file" | tr -d '\r\n')
if [ -z "$access_key" ] || [ -z "$secret_key" ]; then
  echo "密钥文件必须包含 Access Key 和 Secret Key" >&2
  exit 1
fi
case "$access_key$secret_key" in
  *[!A-Za-z0-9._-]*)
    echo "密钥包含不受支持的字符，已停止安装" >&2
    exit 1
    ;;
esac

dnf install -y python3.11 python3.11-pip nginx openssl curl

if ! id ozon-erp >/dev/null 2>&1; then
  useradd --system --home-dir "$state_dir" --shell /sbin/nologin ozon-erp
fi

mkdir -p "$app_dir" "$state_dir/work" "$config_dir" "$nginx_secret_dir"
cp -a "$project_root"/. "$app_dir"/
python3.11 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip setuptools
"$venv_dir/bin/python" -m pip install --no-build-isolation --force-reinstall "$app_dir"

cat >"$config_dir/erp.env" <<EOF
ALPHASHOP_ACCESS_KEY=$access_key
ALPHASHOP_SECRET_KEY=$secret_key
DRY_RUN=true
EOF
chmod 600 "$config_dir/erp.env"
unset access_key secret_key
chown -R ozon-erp:ozon-erp "$state_dir"

cat >/etc/systemd/system/ozon-erp.service <<EOF
[Unit]
Description=1688 to OZON ERP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ozon-erp
Group=ozon-erp
EnvironmentFile=$config_dir/erp.env
WorkingDirectory=$app_dir
ExecStart=$venv_dir/bin/ozon-erp --host 127.0.0.1 --port 18080 --work-root $state_dir/work
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

admin_password=
if [ ! -s "$nginx_secret_dir/htpasswd" ]; then
  admin_password=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)
  printf 'admin:%s\n' "$(openssl passwd -apr1 "$admin_password")" >"$nginx_secret_dir/htpasswd"
fi
chown root:nginx "$nginx_secret_dir/htpasswd"
chmod 640 "$nginx_secret_dir/htpasswd"

if [ ! -s "$nginx_secret_dir/server.key" ] || [ ! -s "$nginx_secret_dir/server.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
    -keyout "$nginx_secret_dir/server.key" \
    -out "$nginx_secret_dir/server.crt" \
    -subj '/CN=8.217.125.205' \
    -addext 'subjectAltName=IP:8.217.125.205' >/dev/null 2>&1
fi
chmod 600 "$nginx_secret_dir/server.key"
if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF "$nginx_secret_dir"
fi

cat >/etc/nginx/conf.d/ozon-erp.conf <<'EOF'
server {
    listen 8443 ssl;
    listen [::]:8443 ssl;
    server_name _;

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
        proxy_read_timeout 300s;
    }
}
EOF

if [ -f /etc/ssh/sshd_config.pre-ozon-deploy ]; then
  cp /etc/ssh/sshd_config.pre-ozon-deploy /etc/ssh/sshd_config
  sshd -t
  systemctl restart sshd
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
  setsebool -P httpd_can_network_connect 1
fi

systemctl daemon-reload
systemctl enable --now ozon-erp
nginx -t
systemctl reload nginx

for attempt in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:18080/api/health >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    systemctl status ozon-erp --no-pager >&2
    exit 1
  fi
  sleep 1
done

echo
echo "OZON ERP 部署完成"
echo "网址：https://8.217.125.205:8443"
echo "用户名：admin"
if [ -n "$admin_password" ]; then
  echo "临时密码：$admin_password"
else
  echo "登录密码：保持现有密码不变"
fi
echo "浏览器会提示自签名证书警告；确认 IP 正确后可继续访问。"
echo "系统保持 DRY_RUN=true，未配置 OZON 凭证前不会执行真实上架。"
