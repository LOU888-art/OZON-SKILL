"""CLI for dry-run assembly and explicitly enabled OZON uploads."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .assembler import assemble_import_payload
from .client import OzonUploadClient
from .config import load_store_configs
from .log import UploadLog
from .workflow import run_upload_workflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble and optionally upload OZON products")
    parser.add_argument("final", type=Path, help="ozon-listing-final.json")
    parser.add_argument("--source", type=Path, required=True, help="normalized 1688 JSON")
    parser.add_argument("--config", type=Path, required=True, help="non-secret upload config")
    parser.add_argument("--payload-output", type=Path, default=Path("upload-dry-run.json"))
    parser.add_argument("--upload-log", type=Path, default=Path("upload_log.json"))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-polls", type=int, default=60)
    return parser


def _read_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    final = _read_object(args.final)
    source = _read_object(args.source)
    config = _read_object(args.config)
    currency = os.environ.get("OZON_CURRENCY")
    if not currency:
        raise SystemExit(
            "OZON_CURRENCY is required and must match the OZON seller account"
        )
    currency = currency.strip().upper()
    if currency not in {"RUB", "CNY"}:
        raise SystemExit("OZON_CURRENCY must be RUB or CNY")
    config["stores"] = [
        {**item, "currency_code": currency}
        for item in config.get("stores") or []
        if isinstance(item, dict)
    ]
    stores = load_store_configs(config)
    execute = os.environ.get("DRY_RUN", "true").strip().lower() == "false"
    upload_log = UploadLog(args.upload_log)

    dry_run_document: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "execute" if execute else "dry_run",
        "generated_at": _now(),
        "stores": [],
    }
    exit_code = 0
    for store in stores:
        assembly = assemble_import_payload(final, source, store)
        dry_run_document["stores"].append(
            {
                "store_id": store.store_id,
                "cny_to_rub_rate": str(store.cny_to_rub_rate),
                "dimension_source": assembly.dimension_source,
                "validation_errors": assembly.validation_errors,
                "warnings": assembly.warnings,
                "payload": assembly.payload,
            }
        )
        client = None
        if execute and not assembly.validation_errors:
            client_id = os.environ.get(f"OZON_{store.credential_prefix}_CLIENT_ID")
            api_key = os.environ.get(f"OZON_{store.credential_prefix}_API_KEY")
            if not client_id or not api_key:
                upload_log.append(
                    {
                        "run_id": None,
                        "created_at": _now(),
                        "store_id": store.store_id,
                        "mode": "execute",
                        "request_sent": False,
                        "offer_ids": assembly.offer_ids,
                        "task_id": None,
                        "status": "MISSING_CREDENTIALS",
                        "error": (
                            f"OZON_{store.credential_prefix}_CLIENT_ID and "
                            f"OZON_{store.credential_prefix}_API_KEY are required"
                        ),
                        "history": [],
                    }
                )
                exit_code = 1
                continue
            client = OzonUploadClient(client_id, api_key)
        try:
            result = run_upload_workflow(
                store=store,
                assembly=assembly,
                upload_log=upload_log,
                execute=execute,
                client=client,
                poll_interval_seconds=args.poll_interval,
                max_polls=args.max_polls,
            )
            if result["status"] in {
                "EXECUTION_BLOCKED",
                "FAILED",
                "POLL_TIMEOUT",
                "VERIFICATION_FAILED",
            }:
                exit_code = 1 if execute else exit_code
        except Exception as exc:
            # A failure for one store must not prevent the remaining stores.
            upload_log.append(
                {
                    "run_id": None,
                    "created_at": _now(),
                    "store_id": store.store_id,
                    "mode": "execute" if execute else "dry_run",
                    "request_sent": False,
                    "offer_ids": assembly.offer_ids,
                    "task_id": None,
                    "status": "STORE_WORKFLOW_ERROR",
                    "error": str(exc)[:500],
                    "history": [],
                }
            )
            exit_code = 1

    with args.payload_output.open("w", encoding="utf-8") as handle:
        json.dump(dry_run_document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    json.dump(dry_run_document, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

