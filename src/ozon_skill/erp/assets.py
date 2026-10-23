"""Persistent metadata for managed image assets and safe cleanup decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class AssetRegistryError(RuntimeError):
    """A safe, user-facing asset registry failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def asset_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def managed_asset_id(object_key: str, public_url: str) -> str:
    return asset_id(f"{str(public_url).strip()}\0{str(object_key).strip().lstrip('/')}")


class AssetRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetRegistryError("图片资产索引无法读取") from exc
        assets = value.get("assets") if isinstance(value, dict) else None
        if not isinstance(assets, dict):
            raise AssetRegistryError("图片资产索引格式无效")
        return {
            str(key): dict(record)
            for key, record in assets.items()
            if isinstance(record, Mapping)
        }

    def _write(self, assets: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"schema_version": "1.0", "updated_at": _now(), "assets": assets},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def upsert(self, value: Mapping[str, Any]) -> dict[str, Any]:
        object_key = str(value.get("object_key") or "").strip().lstrip("/")
        if not object_key:
            raise AssetRegistryError("OSS 对象键不能为空")
        public_url = str(value.get("public_url") or "").strip()
        identifier = managed_asset_id(object_key, public_url)
        with self._lock:
            assets = self._read()
            existing = assets.get(identifier, {})
            record = {
                **existing,
                **dict(value),
                "asset_id": identifier,
                "object_key": object_key,
                "managed": True,
                "first_seen_at": existing.get("first_seen_at") or _now(),
                "updated_at": _now(),
                "published": bool(existing.get("published") or value.get("published")),
            }
            assets[identifier] = record
            self._write(assets)
            return dict(record)

    def synchronize(self, discovered: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            assets = self._read()
            for record in assets.values():
                record["references"] = []
                record["reference_count"] = 0
                record["orphaned"] = True
            for identifier, value in discovered.items():
                existing = assets.get(identifier, {})
                references = list(value.get("references") or [])
                assets[identifier] = {
                    **existing,
                    **dict(value),
                    "asset_id": identifier,
                    "managed": True,
                    "first_seen_at": existing.get("first_seen_at") or _now(),
                    "updated_at": _now(),
                    "published": bool(existing.get("published") or value.get("published")),
                    "references": references,
                    "reference_count": len(references),
                    "orphaned": not references,
                }
            self._write(assets)
            return [dict(record) for record in assets.values()]

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._read().get(str(identifier or ""))
            return dict(record) if record else None

    def delete(self, identifier: str) -> None:
        with self._lock:
            assets = self._read()
            if str(identifier or "") not in assets:
                raise AssetRegistryError("图片资产不存在")
            del assets[str(identifier)]
            self._write(assets)
