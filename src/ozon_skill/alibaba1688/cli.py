"""Command line interface for collecting one 1688 offer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from .client import Alibaba1688Client
from .normalizer import normalize_product


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect one 1688 product")
    parser.add_argument("offer_id", help="1688 num_iid / offer_id")
    parser.add_argument("--output", type=Path, default=Path("sample.json"))
    parser.add_argument(
        "--mock-file",
        type=Path,
        help="Use a saved alibaba.product.get response instead of the network",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mock_file:
        with args.mock_file.open(encoding="utf-8") as handle:
            response = json.load(handle)
    else:
        app_key = os.environ.get("ALIBABA_1688_APP_KEY")
        app_secret = os.environ.get("ALIBABA_1688_APP_SECRET")
        if not app_key or not app_secret:
            raise SystemExit(
                "ALIBABA_1688_APP_KEY and ALIBABA_1688_APP_SECRET are required "
                "unless --mock-file is used"
            )
        client = Alibaba1688Client(
            app_key=app_key,
            app_secret=app_secret,
            access_token=os.environ.get("ALIBABA_1688_ACCESS_TOKEN"),
        )
        response = client.product_get(args.offer_id)

    normalized = normalize_product(
        response,
        args.offer_id,
        collection_mode="mock" if args.mock_file else "live",
    )
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

