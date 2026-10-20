#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行登录重置脚本" >&2
  exit 1
fi

username=${1:-}
if [ -z "$username" ]; then
  printf '新用户名：'
  IFS= read -r username
fi
case "$username" in
  ''|*[!A-Za-z0-9._-]*)
    echo "用户名只能包含字母、数字、点、下划线和连字符" >&2
    exit 1
    ;;
esac

password=${2:-}
if [ -z "$password" ]; then
  printf '新密码（输入时不会显示）：'
  stty -echo
  IFS= read -r password
  stty echo
  printf '\n'
fi
if [ "${#password}" -lt 8 ]; then
  unset password
  echo "密码至少需要 8 个字符" >&2
  exit 1
fi

secret_dir=/etc/nginx/ozon-erp
password_file=$secret_dir/htpasswd
mkdir -p "$secret_dir"
temporary=$(mktemp "$secret_dir/.htpasswd.XXXXXX")
trap 'rm -f "$temporary"' EXIT
printf '%s:%s\n' "$username" "$(openssl passwd -apr1 "$password")" >"$temporary"
unset password
chown root:nginx "$temporary"
chmod 640 "$temporary"
mv "$temporary" "$password_file"
trap - EXIT

nginx -t
systemctl enable nginx
systemctl restart nginx
systemctl enable ozon-erp
systemctl restart ozon-erp

if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port=8443/tcp >/dev/null
  firewall-cmd --reload >/dev/null
fi

for attempt in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:18080/api/health >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    systemctl status ozon-erp nginx --no-pager >&2
    exit 1
  fi
  sleep 1
done

echo "ERP 登录已重置"
echo "网址：https://8.217.125.205:8443/"
echo "用户名：$username"
echo "密码未输出，请使用刚才输入的密码。"
