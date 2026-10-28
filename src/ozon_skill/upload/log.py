"""Atomic local upload log with one independent entry per store run."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class UploadLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "entries": []}
        with self.path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            raise ValueError("upload log has an invalid structure")
        return value

    def has_task_for_offer(self, store_id: str, offer_id: str) -> bool:
        retryable_terminal_statuses = {
            "FAILED",
            "VERIFICATION_FAILED",
            "ATTRIBUTE_VERIFY_ERROR",
            "INFO_LIST_ERROR",
            "UNARCHIVE_ERROR",
        }
        uncertain_submission_statuses = {
            "SUBMITTING",
            "SUBMISSION_OUTCOME_UNKNOWN",
            "IMPORT_RESPONSE_MISSING_TASK_ID",
        }
        return any(
            isinstance(entry, dict)
            and entry.get("store_id") == store_id
            and (
                entry.get("task_id") is not None
                or entry.get("status") in uncertain_submission_statuses
            )
            and offer_id in (entry.get("offer_ids") or [])
            and entry.get("status") not in retryable_terminal_statuses
            for entry in self.data["entries"]
        )

    def has_uncertain_submission_for_offer(
        self, store_id: str, offer_id: str
    ) -> bool:
        """Block only requests whose original submission outcome is unknown.

        A known Ozon task is safe to update with the same offer_id: product
        import is an upsert.  Treating every known task as a duplicate used to
        make it impossible to repair images on an already-created product.
        """

        uncertain_statuses = {
            "SUBMITTING",
            "SUBMISSION_OUTCOME_UNKNOWN",
            "IMPORT_RESPONSE_MISSING_TASK_ID",
        }
        return any(
            isinstance(entry, dict)
            and entry.get("store_id") == store_id
            and offer_id in (entry.get("offer_ids") or [])
            and entry.get("status") in uncertain_statuses
            and not isinstance(entry.get("task_id"), int)
            for entry in self.data["entries"]
        )

    def latest_for_payload(
        self, store_id: str, payload_sha256: str
    ) -> dict[str, Any] | None:
        for entry in reversed(self.data["entries"]):
            if (
                isinstance(entry, dict)
                and entry.get("store_id") == store_id
                and entry.get("payload_sha256") == payload_sha256
                and entry.get("request_sent") is True
            ):
                return dict(entry)
        return None

    def append(self, entry: dict[str, Any]) -> int:
        self.data["entries"].append(entry)
        self.save()
        return len(self.data["entries"]) - 1

    def update(self, index: int, **values: Any) -> None:
        self.data["entries"][index].update(values)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

