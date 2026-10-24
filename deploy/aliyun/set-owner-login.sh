#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行主账号设置脚本" >&2
  exit 1
fi

identifier=${1:-}
if [ -z "$identifier" ]; then
  printf '主账号邮箱或手机号：'
  IFS= read -r identifier
fi

password=${2:-}
if [ -z "$password" ]; then
  printf '主账号密码（至少 8 位，包含字母和数字；输入时不会显示）：'
  stty -echo
  IFS= read -r password
  stty echo
  printf '\n'
fi

python_bin=/opt/ozon-erp/venv/bin/python
auth_file=/var/lib/ozon-erp/work/.auth.json
team_file=/var/lib/ozon-erp/work/.team.json
if [ ! -x "$python_bin" ] || [ ! -s "$auth_file" ] || [ ! -s "$team_file" ]; then
  echo "未找到已安装的 ERP 应用账户或团队数据，请先完成新版部署" >&2
  exit 1
fi

owner_username=$(
  "$python_bin" -c 'import json, pathlib; p=pathlib.Path("/var/lib/ozon-erp/work/.team.json"); d=json.loads(p.read_text()); print(next((str(x.get("username")) for x in d.get("members", []) if x.get("role")=="owner"), ""))'
)
if [ -z "$owner_username" ]; then
  echo "团队数据中没有工作区所有者，已停止修改" >&2
  exit 1
fi

runuser -u ozon-erp -- "$python_bin" -m ozon_skill.erp.auth \
  --path "$auth_file" \
  --identifier "$identifier" \
  --password "$password" \
  --username "$owner_username" \
  --primary
unset password

echo "主账号登录信息已更新，原主账号会话已全部撤销。"
echo "主账号：$identifier"
echo "权限：工作区所有者（全部权限）"
echo "登录地址：https://8.217.125.205/login"
