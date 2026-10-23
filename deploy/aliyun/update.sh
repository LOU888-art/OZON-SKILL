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
"$venv_dir/bin/python" -m pip install \
  --no-build-isolation \
  --no-deps \
  --force-reinstall \
  "$app_dir"

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

revision=$(git -C "$project_root" rev-parse --short HEAD 2>/dev/null || true)
echo
echo "OZON ERP 更新完成${revision:+，版本 $revision}"
echo "运行页面：$runtime_index"
echo "服务状态：$(systemctl is-active ozon-erp)"
echo "请打开 https://8.217.125.205/ 并按 Ctrl+F5 强制刷新。"
