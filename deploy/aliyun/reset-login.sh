#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行登录重置脚本" >&2
  exit 1
fi

identifier=${1:-}
if [ -z "$identifier" ]; then
  printf '注册邮箱或手机号：'
  IFS= read -r identifier
fi

password=${2:-}
if [ -z "$password" ]; then
  printf '新密码（至少 8 位，包含字母和数字；输入时不会显示）：'
  stty -echo
  IFS= read -r password
  stty echo
  printf '\n'
fi
python_bin=/opt/ozon-erp/venv/bin/python
auth_file=/var/lib/ozon-erp/work/.auth.json
if [ ! -x "$python_bin" ] || [ ! -s "$auth_file" ]; then
  echo "未找到已安装的 ERP 应用账户数据" >&2
  exit 1
fi

runuser -u ozon-erp -- "$python_bin" -m ozon_skill.erp.auth \
  --path "$auth_file" \
  --identifier "$identifier" \
  --password "$password" \
  --reset
unset password

echo "应用登录密码已重置，原有登录会话已全部撤销。"
echo "网址：https://8.217.125.205/login"
echo "登录账号：$identifier"
