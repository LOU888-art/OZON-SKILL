#!/usr/bin/env bash
set -eu

python_bin=${1:-/opt/ozon-erp/venv/bin/python}

if ! command -v tesseract >/dev/null 2>&1; then
  echo "OCR自检失败：没有安装 tesseract" >&2
  exit 1
fi

languages=$(tesseract --list-langs 2>&1) || {
  echo "OCR自检失败：tesseract 无法读取语言包" >&2
  echo "$languages" >&2
  exit 1
}
for language in chi_sim rus eng; do
  if ! printf '%s\n' "$languages" | grep -qx "$language"; then
    echo "OCR自检失败：缺少 $language 语言包" >&2
    exit 1
  fi
done

result=$(
  "$python_bin" -c '
from io import BytesIO
from PIL import Image
from ozon_skill.content_generation.image_quality import detect_chinese_text
image = Image.new("RGB", (300, 400), "white")
payload = BytesIO()
image.save(payload, format="PNG")
result = detect_chinese_text(payload.getvalue())
print(result.get("ocr_status"), result.get("contains_chinese"))
' 2>&1
) || {
  echo "OCR自检失败：ERP进程无法执行图片识别" >&2
  echo "$result" >&2
  exit 1
}

if [ "$result" != "COMPLETED False" ]; then
  echo "OCR自检失败：预期 COMPLETED False，实际 $result" >&2
  exit 1
fi

echo "OCR自检通过：chi_sim + rus + eng，双模式识别可执行"
