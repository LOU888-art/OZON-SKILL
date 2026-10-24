"""Persistent OZON Seller API store authorizations for the ERP.

The API key is stored in a mode-0600 file owned by the ERP service user and is
never returned by public methods.  Credential validation uses an OZON read-only
description-category request.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ozon_skill.ozon.client import OzonDescriptionCategoryClient


_STORE_ID = re.compile(r"^[A-Za-z0-9._-]{1,48}$")


class StoreAuthorizationError(RuntimeError):
    """A user-facing store authorization or persistence failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StoreRegistry:
    def __init__(
        self,
        path: Path,
        *,
        client_factory: Callable[[str, str], OzonDescriptionCategoryClient] = OzonDescriptionCategoryClient,
    ) -> None:
        self.path = path.resolve()
        self.client_factory = client_factory
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "stores": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreAuthorizationError("OZON 店铺授权文件无法读取") from exc
        if not isinstance(value, dict) or not isinstance(value.get("stores"), list):
            raise StoreAuthorizationError("OZON 店铺授权文件格式无效")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _public(store: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "store_id": store.get("store_id"),
            "name": store.get("name"),
            "client_id": store.get("client_id"),
            "currency_code": store.get("currency_code"),
            "api_key_configured": bool(store.get("api_key")),
            "verified_at": store.get("verified_at"),
            "created_at": store.get("created_at"),
            "updated_at": store.get("updated_at"),
        }

    @staticmethod
    def _owned_by(store: Mapping[str, Any], owner_username: str | None) -> bool:
        if owner_username is None:
            return True
        return str(store.get("owner_username") or "").casefold() == owner_username.casefold()

    def list_public(self, *, owner_username: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public(item)
                for item in self._read()["stores"]
                if self._owned_by(item, owner_username)
            ]

    def credentials(
        self, store_id: str, *, owner_username: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            for store in self._read()["stores"]:
                if store.get("store_id") == store_id and self._owned_by(
                    store, owner_username
                ):
                    return dict(store)
        raise StoreAuthorizationError(f"未找到 OZON 店铺授权：{store_id}")

    def authorize(
        self, value: Mapping[str, Any], *, owner_username: str | None = None
    ) -> dict[str, Any]:
        store_id = str(value.get("store_id") or "").strip()
        name = str(value.get("name") or "").strip()
        client_id = str(value.get("client_id") or "").strip()
        api_key = str(value.get("api_key") or "").strip()
        currency = str(value.get("currency_code") or "RUB").strip().upper()
        if not _STORE_ID.fullmatch(store_id):
            raise StoreAuthorizationError("店铺标识只能包含字母、数字、点、下划线和连字符")
        if not name:
            raise StoreAuthorizationError("店铺名称不能为空")
        if not client_id or not api_key:
            raise StoreAuthorizationError("Client-Id 和 Api-Key 均不能为空")
        if currency not in {"RUB", "CNY"}:
            raise StoreAuthorizationError("店铺币种仅支持 RUB 或 CNY")

        try:
            self.client_factory(client_id, api_key).category_tree(language="RU")
        except Exception as exc:
            raise StoreAuthorizationError(f"OZON 凭证验证失败：{exc}") from exc

        with self._lock:
            document = self._read()
            stores = document["stores"]
            existing = next(
                (item for item in stores if item.get("store_id") == store_id), None
            )
            if existing is not None and not self._owned_by(existing, owner_username):
                raise StoreAuthorizationError("该店铺标识已由其他账号绑定")
            timestamp = _now()
            record = {
                "store_id": store_id,
                "name": name,
                "client_id": client_id,
                "api_key": api_key,
                "currency_code": currency,
                "verified_at": timestamp,
                "created_at": existing.get("created_at") if existing else timestamp,
                "updated_at": timestamp,
                "owner_username": (
                    owner_username
                    if owner_username is not None
                    else existing.get("owner_username") if existing else None
                ),
            }
            if existing is None:
                stores.append(record)
            else:
                stores[stores.index(existing)] = record
            self._write(document)
            return self._public(record)

    def verify(
        self, store_id: str, *, owner_username: str | None = None
    ) -> dict[str, Any]:
        store = self.credentials(store_id, owner_username=owner_username)
        try:
            self.client_factory(store["client_id"], store["api_key"]).category_tree(
                language="RU"
            )
        except Exception as exc:
            raise StoreAuthorizationError(f"OZON 凭证验证失败：{exc}") from exc
        with self._lock:
            document = self._read()
            for item in document["stores"]:
                if item.get("store_id") == store_id and self._owned_by(
                    item, owner_username
                ):
                    item["verified_at"] = _now()
                    item["updated_at"] = item["verified_at"]
                    self._write(document)
                    return self._public(item)
        raise StoreAuthorizationError(f"未找到 OZON 店铺授权：{store_id}")

    def delete(self, store_id: str, *, owner_username: str | None = None) -> None:
        with self._lock:
            document = self._read()
            remaining = [
                item
                for item in document["stores"]
                if not (
                    item.get("store_id") == store_id
                    and self._owned_by(item, owner_username)
                )
            ]
            if len(remaining) == len(document["stores"]):
                raise StoreAuthorizationError(f"未找到 OZON 店铺授权：{store_id}")
            document["stores"] = remaining
            self._write(document)
