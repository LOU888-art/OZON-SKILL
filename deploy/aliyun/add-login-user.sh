#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行成员登录脚本" >&2
  exit 1
fi

username=${1:-}
if [ -z "$username" ]; then
  printf '成员登录用户名：'
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
  printf '成员登录密码（输入时不会显示）：'
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
if [ -r "$password_file" ]; then
  awk -F: -v name="$username" '$1 != name' "$password_file" >"$temporary"
fi
printf '%s:%s\n' "$username" "$(openssl passwd -apr1 "$password")" >>"$temporary"
unset password
chown root:nginx "$temporary"
chmod 640 "$temporary"
mv "$temporary" "$password_file"
trap - EXIT

nginx -t
systemctl reload nginx
echo "成员登录凭证已创建：$username"
echo "还需由 ERP 所有者在“账户与套餐 → 团队成员”中添加同名成员并分配角色。"
