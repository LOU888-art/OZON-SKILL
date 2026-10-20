"""Collect one 1688 offer through the real AlphaShop detail API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .client import AlphaShopApiError, AlphaShopClient, extract_offer_id
from .normalizer import normalize_alphashop_offer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect a 1688 offer via AlphaShop")
    parser.add_argument("offer", help="Numeric offer ID or detail.1688.com URL")
    parser.add_argument("--output", type=Path, default=Path("01_raw.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    offer_id = extract_offer_id(args.offer)
    try:
        exchange = AlphaShopClient.from_environment().offer_detail_exchange(offer_id)
    except AlphaShopApiError as exc:
        raise SystemExit(str(exc)) from exc
    result = normalize_alphashop_offer(exchange.payload, offer_id)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
