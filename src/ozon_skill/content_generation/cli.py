"""CLI for Russian copy generation and strict media processing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ozon_skill.alphashop.client import AlphaShopClient

from .generator import build_final_listing
from .media import translate_all_images


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Russian OZON listing content")
    parser.add_argument("prelisting", type=Path)
    parser.add_argument("--source", type=Path, required=True, help="Normalized 1688 JSON")
    parser.add_argument("--output", type=Path, default=Path("ozon-listing-final.json"))
    return parser


def _read_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


def _image_items(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for key in ("images", "detail_images"):
        for item in source.get(key) or []:
            if isinstance(item, Mapping) and item.get("url") and item["url"] not in seen:
                seen.add(str(item["url"]))
                result.append({**item, "source_role": key})
    for sku in source.get("sku_list") or []:
        if not isinstance(sku, Mapping) or not sku.get("image_url"):
            continue
        url = str(sku["image_url"])
        result.append(
            {
                "url": url,
                "source_role": "sku_image",
                "sku_id": sku.get("sku_id"),
                "specification_zh": sku.get("specification_zh")
                or sku.get("specification"),
            }
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prelisting = _read_object(args.prelisting)
    source = _read_object(args.source)
    inspection = translate_all_images(
        _image_items(source), AlphaShopClient.from_environment()
    )
    result = build_final_listing(prelisting, source, inspection)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

