"""CLI for the category and attribute mapping stage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from ozon_skill.alphashop.client import AlphaShopClient, extract_external_category_pair
from ozon_skill.ozon.client import OzonDescriptionCategoryClient

from .mapper import CONFIDENCE_THRESHOLD, map_listing
from .provider import FixtureMetadataProvider, LiveMetadataProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Map normalized 1688 data to OZON metadata")
    parser.add_argument("input", type=Path, help="Normalized 1688 JSON")
    parser.add_argument("--output", type=Path, default=Path("ozon-listing-pre.json"))
    parser.add_argument("--mock-dir", type=Path, help="Directory containing OZON API fixtures")
    parser.add_argument("--confidence-threshold", type=float, default=CONFIDENCE_THRESHOLD)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with args.input.open(encoding="utf-8") as handle:
        source = json.load(handle)
    if not isinstance(source, dict):
        raise SystemExit("input must contain a JSON object")

    if args.mock_dir:
        provider = FixtureMetadataProvider(args.mock_dir)
        preferred_pair = None
    else:
        client_id = os.environ.get("OZON_CLIENT_ID")
        api_key = os.environ.get("OZON_API_KEY")
        if not client_id or not api_key:
            raise SystemExit(
                "OZON_CLIENT_ID and OZON_API_KEY are required unless --mock-dir is used"
            )
        provider = LiveMetadataProvider(OzonDescriptionCategoryClient(client_id, api_key))
        source_category = source.get("category_id")
        if source_category is None and isinstance(source.get("category_path_zh"), dict):
            source_category = source["category_path_zh"].get("selected_category_id")
        exchange = AlphaShopClient.from_environment().category_mapping(
            str(source_category or "")
        )
        preferred_pair = extract_external_category_pair(exchange.payload)

    result = map_listing(
        source,
        provider,
        confidence_threshold=args.confidence_threshold,
        preferred_category_type=preferred_pair,
    )
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

