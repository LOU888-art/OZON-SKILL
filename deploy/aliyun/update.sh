#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行更新器" >&2
  exit 1
fi

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
app_dir=/opt/ozon-erp/app
venv_dir=/opt/ozon-erp/venv

if [ ! -x "$venv_dir/bin/python" ] || [ ! -f /etc/systemd/system/ozon-erp.service ]; then
  echo "尚未发现已安装的 ERP，请先运行 bash deploy/aliyun/install.sh" >&2
  exit 1
fi

mkdir -p "$app_dir"
cp -a "$project_root"/. "$app_dir"/
if ! "$venv_dir/bin/python" -c 'import PIL' >/dev/null 2>&1; then
  "$venv_dir/bin/python" -m pip install "Pillow>=10,<13"
fi
if ! tesseract --list-langs 2>/dev/null | grep -qx 'chi_sim'; then
  dnf install -y tesseract tesseract-langpack-chi_sim tesseract-langpack-rus
elif ! tesseract --list-langs 2>/dev/null | grep -qx 'rus'; then
  dnf install -y tesseract-langpack-rus
fi
"$venv_dir/bin/python" -m pip install \
  --no-build-isolation \
  --no-deps \
  --force-reinstall \
  "$app_dir"
bash "$project_root/deploy/aliyun/verify-ocr.sh" "$venv_dir/bin/python"

state_dir=/var/lib/ozon-erp
auth_file=$state_dir/work/.auth.json
bootstrap_password=
bootstrap_identifier=
if [ ! -s "$auth_file" ]; then
  owner_username=$(
    "$venv_dir/bin/python" -c 'import json, pathlib; p=pathlib.Path("/var/lib/ozon-erp/work/.team.json"); d=json.loads(p.read_text()) if p.exists() else {}; print(next((str(x.get("username")) for x in d.get("members", []) if x.get("role")=="owner"), "admin"))'
  )
  bootstrap_identifier=admin@northstar.local
  bootstrap_password=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)
  runuser -u ozon-erp -- "$venv_dir/bin/python" -m ozon_skill.erp.auth \
    --path "$auth_file" \
    --identifier "$bootstrap_identifier" \
    --password "$bootstrap_password" \
    --username "$owner_username"
fi

systemctl restart ozon-erp

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent http://127.0.0.1:18080/api/health >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 10 ]; then
    systemctl status ozon-erp --no-pager >&2
    exit 1
  fi
  sleep 1
done

bash "$project_root/deploy/aliyun/configure-nginx.sh"

runtime_index=$(
  "$venv_dir/bin/python" -c \
    'from pathlib import Path; import ozon_skill.erp.app as app; print(Path(app.__file__).with_name("static").joinpath("index.html"))'
)
if ! grep -q 'id="storesNav"' "$runtime_index"; then
  echo "运行包校验失败：没有发现第 08 项 OZON 店铺导航" >&2
  exit 1
fi
if ! grep -q '只保留 AI 优化图与俄语翻译图' "$runtime_index"; then
  echo "运行包校验失败：没有发现新版图片资产页面" >&2
  exit 1
fi
if [ ! -f "$app_dir/browser-extension/northstar-1688-collector/manifest.json" ]; then
  echo "运行包校验失败：没有发现1688浏览器采集插件" >&2
  exit 1
fi

revision=$(git -C "$project_root" rev-parse --short HEAD 2>/dev/null || true)
echo
echo "OZON ERP 更新完成${revision:+，版本 $revision}"
echo "运行页面：$runtime_index"
echo "服务状态：$(systemctl is-active ozon-erp)"
if [ -n "$bootstrap_password" ]; then
  echo "首次应用登录邮箱：$bootstrap_identifier"
  echo "首次应用登录密码：$bootstrap_password"
  echo "请立即登录并妥善保存；原浏览器 Basic Auth 密码已停止使用。"
fi
echo "请打开 https://8.217.125.205/ 并按 Ctrl+F5 强制刷新。"
