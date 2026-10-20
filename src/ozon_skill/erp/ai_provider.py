"""Private AI relay configuration for the ERP; API keys are never returned."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from ozon_skill.ai_relay.client import (
    DEFAULT_BASE_URL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_TEXT_MODEL,
    AiRelayClient,
    AiRelayError,
    _https_base_url,
    normalize_image_model,
)


class AiProviderConfigurationError(RuntimeError):
    """User-facing AI provider configuration error."""


class AiProviderRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AiProviderConfigurationError("AI 服务配置文件无法读取") from exc
        if not isinstance(value, dict):
            raise AiProviderConfigurationError("AI 服务配置文件格式无效")
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
    def _public(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
        return {
            "source": source,
            "base_url": value.get("base_url", DEFAULT_BASE_URL),
            "text_model": value.get("text_model", DEFAULT_TEXT_MODEL),
            "image_model": normalize_image_model(
                str(value.get("image_model", DEFAULT_IMAGE_MODEL))
            ),
            "text_api_key_configured": bool(value.get("text_api_key")),
            "image_api_key_configured": bool(value.get("image_api_key")),
        }

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            stored = self._read()
        if stored:
            return self._public(stored, source="private_config")
        client = AiRelayClient.from_environment()
        return {**client.public_status(), "source": "environment"}

    def save(self, value: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._read()
            base_url = _https_base_url(
                str(value.get("base_url") or existing.get("base_url") or DEFAULT_BASE_URL)
            )
            text_model = str(
                value.get("text_model") or existing.get("text_model") or DEFAULT_TEXT_MODEL
            ).strip()
            image_model = normalize_image_model(str(
                value.get("image_model")
                or existing.get("image_model")
                or DEFAULT_IMAGE_MODEL
            ))
            text_key = str(value.get("text_api_key") or "").strip() or existing.get(
                "text_api_key"
            )
            image_key = str(value.get("image_api_key") or "").strip() or existing.get(
                "image_api_key"
            )
            if not text_model or not image_model:
                raise AiProviderConfigurationError("文本模型和生图模型不能为空")
            if not text_key or not image_key:
                raise AiProviderConfigurationError("文本与生图 API Key 均不能为空")
            record = {
                "base_url": base_url,
                "text_model": text_model,
                "image_model": image_model,
                "text_api_key": text_key,
                "image_api_key": image_key,
            }
            self._write(record)
            return self._public(record, source="private_config")

    def client(self) -> AiRelayClient:
        with self._lock:
            stored = self._read()
        if not stored:
            return AiRelayClient.from_environment()
        try:
            return AiRelayClient(
                base_url=_https_base_url(str(stored["base_url"])),
                text_api_key=str(stored["text_api_key"]),
                image_api_key=str(stored["image_api_key"]),
                text_model=str(stored["text_model"]),
                image_model=normalize_image_model(str(stored["image_model"])),
            )
        except (KeyError, AiRelayError) as exc:
            raise AiProviderConfigurationError("AI 服务私有配置不完整") from exc

    def delete(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
