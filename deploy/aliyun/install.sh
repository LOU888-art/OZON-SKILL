#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行安装器" >&2
  exit 1
fi

secret_file=${1:-/root/秘钥.txt}
existing_env=/etc/ozon-erp/erp.env

if [ -r "$secret_file" ]; then
  chmod 600 "$secret_file"
  access_key=$(sed -n 's/^[[:space:]]*Access Key[[:space:]]*[:：][[:space:]]*//p' "$secret_file" | tr -d '\r\n')
  secret_key=$(sed -n 's/^[[:space:]]*Secret Key[[:space:]]*[:：][[:space:]]*//p' "$secret_file" | tr -d '\r\n')
  credential_source=$secret_file
elif [ -r "$existing_env" ]; then
  access_key=$(sed -n 's/^[[:space:]]*ALPHASHOP_ACCESS_KEY[[:space:]]*=[[:space:]]*//p' "$existing_env" | tail -n 1 | tr -d '\r\n')
  secret_key=$(sed -n 's/^[[:space:]]*ALPHASHOP_SECRET_KEY[[:space:]]*=[[:space:]]*//p' "$existing_env" | tail -n 1 | tr -d '\r\n')
  credential_source=$existing_env
  echo "未找到 $secret_file，正在复用现有 ERP 密钥配置。"
else
  echo "找不到密钥文件 $secret_file，且没有可复用的 $existing_env" >&2
  exit 1
fi

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
install_root=/opt/ozon-erp
app_dir=$install_root/app
venv_dir=$install_root/venv
state_dir=/var/lib/ozon-erp
config_dir=/etc/ozon-erp
nginx_secret_dir=/etc/nginx/ozon-erp

if [ -z "$access_key" ] || [ -z "$secret_key" ]; then
  echo "$credential_source 中缺少有效的 AlphaShop Access Key 或 Secret Key" >&2
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

dry_run=true
erp_plan=business
if [ -r "$config_dir/erp.env" ]; then
  previous_dry_run=$(sed -n 's/^[[:space:]]*DRY_RUN[[:space:]]*=[[:space:]]*//p' "$config_dir/erp.env" | tail -n 1 | tr -d '\r')
  case "$previous_dry_run" in
    true|false) dry_run=$previous_dry_run ;;
  esac
  previous_erp_plan=$(sed -n 's/^[[:space:]]*ERP_PLAN[[:space:]]*=[[:space:]]*//p' "$config_dir/erp.env" | tail -n 1 | tr -d '\r')
  case "$previous_erp_plan" in
    starter|growth|business) erp_plan=$previous_erp_plan ;;
  esac
fi
cat >"$config_dir/erp.env" <<EOF
ALPHASHOP_ACCESS_KEY=$access_key
ALPHASHOP_SECRET_KEY=$secret_key
DRY_RUN=$dry_run
ERP_PLAN=$erp_plan
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

if [ -f /etc/ssh/sshd_config.pre-ozon-deploy ]; then
  cp /etc/ssh/sshd_config.pre-ozon-deploy /etc/ssh/sshd_config
  sshd -t
  systemctl restart sshd
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
  setsebool -P httpd_can_network_connect 1
fi

systemctl daemon-reload
systemctl enable ozon-erp
systemctl restart ozon-erp
nginx -t
systemctl enable nginx
systemctl restart nginx

if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=https >/dev/null
  firewall-cmd --permanent --add-port=8443/tcp >/dev/null
  firewall-cmd --reload >/dev/null
fi

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
echo "网址：https://8.217.125.205/"
echo "用户名：admin"
if [ -n "$admin_password" ]; then
  echo "临时密码：$admin_password"
else
  echo "登录密码：保持现有密码不变"
fi
echo "浏览器会提示自签名证书警告；确认 IP 正确后可继续访问。"
if [ "$dry_run" = "false" ]; then
  echo "已保留现有 LIVE MODE（DRY_RUN=false）；网页仍需再次勾选确认才会真实提交。"
else
  echo "系统保持 DRY_RUN=true，未配置 OZON 凭证前不会执行真实上架。"
fi
