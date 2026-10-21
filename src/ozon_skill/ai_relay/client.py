"""Strict OpenAI-compatible relay client without credential logging."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://gpt.gptzzz.xyz/v1"
DEFAULT_TEXT_MODEL = "gpt-5.6-sol"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
_LEGACY_IMAGE_MODELS = {"GPT-image-2.0": DEFAULT_IMAGE_MODEL}


class AiRelayError(RuntimeError):
    """The relay returned an unusable or unsafe response."""


def normalize_image_model(value: str) -> str:
    """Migrate the previously configured relay model name without touching keys."""

    model = value.strip()
    return _LEGACY_IMAGE_MODELS.get(model, model)


def _model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def normalize_text_model(value: str) -> str:
    """Normalize historical display names to the relay's canonical model ID."""

    model = value.strip()
    if _model_key(model) == _model_key(DEFAULT_TEXT_MODEL):
        return DEFAULT_TEXT_MODEL
    return model


def _safe_error_detail(raw: str) -> tuple[str | None, str | None]:
    """Extract only upstream error metadata and redact credential-like text."""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    error = payload.get("error")
    source = error if isinstance(error, Mapping) else payload
    code = source.get("code") or source.get("type") or payload.get("code")
    message = source.get("message") or payload.get("message")
    safe_code = str(code)[:80] if code is not None else None
    if message is None:
        return safe_code, None
    safe_message = str(message)[:300]
    safe_message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", safe_message)
    safe_message = re.sub(
        r"(?i)(authorization|api[-_ ]?key|bearer)\s*[:=]?\s*\S+",
        r"\1 [REDACTED]",
        safe_message,
    )
    return safe_code, safe_message


def _https_base_url(value: str) -> str:
    result = value.strip().rstrip("/")
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AiRelayError("AI 中转站地址必须是无账号信息的 HTTPS URL")
    return result


def _json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AiRelayError("AI 文本响应不是合法 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise AiRelayError("AI 文本响应顶层必须是 JSON 对象")
    return parsed


def _message_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise AiRelayError("AI 文本响应缺少 choices[0]")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    raise AiRelayError("AI 文本响应缺少 message.content")


def _image_url(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        raise AiRelayError("AI 生图响应缺少 data[0]")
    url = data[0].get("url")
    if isinstance(url, str) and url.startswith("https://"):
        return url
    if data[0].get("b64_json"):
        raise AiRelayError("AI 生图仅返回 Base64，当前需要中转站返回公开 HTTPS URL")
    raise AiRelayError("AI 生图响应缺少公开 HTTPS URL")


@dataclass(frozen=True)
class AiRelayClient:
    base_url: str
    text_api_key: str | None
    image_api_key: str | None
    text_model: str = DEFAULT_TEXT_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    timeout_seconds: float = 180.0

    @classmethod
    def from_environment(cls) -> "AiRelayClient":
        return cls(
            base_url=_https_base_url(
                os.environ.get("AI_RELAY_BASE_URL", DEFAULT_BASE_URL)
            ),
            text_api_key=(os.environ.get("AI_RELAY_TEXT_API_KEY") or "").strip() or None,
            image_api_key=(os.environ.get("AI_RELAY_IMAGE_API_KEY") or "").strip() or None,
            text_model=(
                normalize_text_model(
                    os.environ.get("AI_RELAY_TEXT_MODEL", DEFAULT_TEXT_MODEL)
                )
                or DEFAULT_TEXT_MODEL
            ),
            image_model=(
                normalize_image_model(
                    os.environ.get("AI_RELAY_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)
                )
                or DEFAULT_IMAGE_MODEL
            ),
        )

    def _post(
        self,
        path: str,
        api_key: str | None,
        body: Mapping[str, Any],
        *,
        operation: str,
        model: str,
    ) -> dict[str, Any]:
        if not api_key:
            raise AiRelayError("AI 中转站对应的 API Key 尚未配置")
        request = Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ozon-skill-ai-relay/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            raw = exc.read(16 * 1024).decode("utf-8", errors="replace")
            code, message = _safe_error_detail(raw)
            detail = f"；code={code}" if code else ""
            detail += f"；message={message}" if message else ""
            if exc.code == 503:
                detail += "；上游暂无可用通道，请核对模型名称或稍后重试"
            raise AiRelayError(
                f"AI 中转站{operation}请求失败：HTTP {exc.code}；model={model}{detail}"
            ) from exc
        except URLError as exc:
            raise AiRelayError(f"AI 中转站{operation}连接失败：{exc.reason}") from exc
        if status != 200:
            raise AiRelayError(
                f"AI 中转站{operation}请求失败：HTTP {status}；model={model}"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AiRelayError("AI 中转站返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise AiRelayError("AI 中转站响应顶层不是对象")
        if payload.get("error"):
            error = payload["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            raise AiRelayError(f"AI 中转站返回错误：{code or 'UNKNOWN'}")
        return payload

    def _available_models(self, api_key: str | None) -> list[str]:
        """Read model IDs without ever exposing the credential in errors or logs."""

        if not api_key:
            raise AiRelayError("AI 中转站对应的 API Key 尚未配置")
        request = Request(
            f"{self.base_url}/models",
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "ozon-skill-ai-relay/1.0",
            },
        )
        try:
            with urlopen(request, timeout=min(self.timeout_seconds, 30.0)) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            raise AiRelayError(f"AI 中转站模型列表查询失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise AiRelayError(f"AI 中转站模型列表连接失败：{exc.reason}") from exc
        if status != 200:
            raise AiRelayError(f"AI 中转站模型列表查询失败：HTTP {status}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AiRelayError("AI 中转站模型列表不是 JSON") from exc
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            raise AiRelayError("AI 中转站模型列表缺少 data")
        return [
            str(item["id"]).strip()
            for item in data
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        ]

    def _resolve_text_model(self, configured: str) -> str:
        models = self._available_models(self.text_api_key)
        if configured in models:
            return configured
        matches = [
            model for model in models if _model_key(model) == _model_key(configured)
        ]
        if len(matches) == 1:
            return matches[0]
        examples = ", ".join(models[:8]) or "（空列表）"
        raise AiRelayError(
            f"AI 中转站不存在文本模型 {configured}；可用模型示例：{examples}"
        )

    def generate_listing_copy(
        self, source: Mapping[str, Any], mapped: Mapping[str, Any]
    ) -> dict[str, Any]:
        facts = {
            "title_zh": source.get("title_zh"),
            "category_path_zh": source.get("category_path_zh"),
            "attributes_zh": source.get("attributes_zh"),
            "sku_specs": [
                item.get("specification") or item.get("specification_zh")
                for item in (source.get("sku_list") or [])[:20]
                if isinstance(item, Mapping)
            ],
            "ozon_attributes": [
                {
                    "name_ru": item.get("attribute_name_ru"),
                    "value": item.get("value"),
                    "dictionary_value_id": item.get("dictionary_value_id"),
                }
                for item in (mapped.get("ozon_attributes") or [])
                if isinstance(item, Mapping)
                and (item.get("value") is not None or item.get("dictionary_value_id") is not None)
            ],
        }
        prompt = (
            "Ты редактор карточек OZON. Используй только факты из входного JSON. "
            "Бренда нет: не придумывай бренд и укажи сегмент brand как 'без бренда'. "
            "Верни только JSON с ключами title_ru, title_segments, description_ru, tags_ru. "
            "title_segments должен содержать core_product_name, key_attributes, "
            "use_case_or_core_benefit, brand. Заголовок: естественный русский язык, "
            "150–200 символов, core_product_name в первых 60 символах, без повторения "
            "одного корня 3 раза. tags_ru — уникальный массив максимум из 30 русских "
            "тегов. Не добавляй неподтвержденные характеристики.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        )
        text_model = normalize_text_model(self.text_model) or DEFAULT_TEXT_MODEL

        def request_copy(model: str) -> dict[str, Any]:
            return self._post(
                "/chat/completions",
                self.text_api_key,
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Return strict JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
                operation="文本生成",
                model=model,
            )

        try:
            response = request_copy(text_model)
        except AiRelayError as exc:
            if "model_not_found" not in str(exc).lower():
                raise
            resolved_model = self._resolve_text_model(text_model)
            if resolved_model == text_model:
                raise
            response = request_copy(resolved_model)
        return _json_object(_message_text(response))

    def translate_attribute_values(
        self, attributes: Sequence[Mapping[str, Any]]
    ) -> dict[str, str]:
        """Translate user-entered Chinese attribute values to factual Russian."""

        fields = [
            {
                "attribute_id": str(item.get("attribute_id")),
                "attribute_name_ru": str(item.get("attribute_name_ru") or ""),
                "value_zh": str(item.get("value_zh") or ""),
            }
            for item in attributes
            if item.get("attribute_id") is not None and item.get("value_zh")
        ]
        if not fields:
            return {}
        prompt = (
            "Translate only the supplied Chinese Ozon attribute values into natural "
            "Russian. Preserve numbers, units, URLs, model codes and factual meaning. "
            "Do not add facts. Return strict JSON: {\"translations\": "
            "{\"attribute_id\": \"russian value\"}}. Return every input ID exactly "
            "once and no extra keys.\nFIELDS:\n"
            + json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        )
        text_model = normalize_text_model(self.text_model) or DEFAULT_TEXT_MODEL

        def request_translation(model: str) -> dict[str, Any]:
            return self._post(
                "/chat/completions",
                self.text_api_key,
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Return strict JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
                operation="特征俄语翻译",
                model=model,
            )

        try:
            response = request_translation(text_model)
        except AiRelayError as exc:
            if "model_not_found" not in str(exc).lower():
                raise
            resolved_model = self._resolve_text_model(text_model)
            if resolved_model == text_model:
                raise
            response = request_translation(resolved_model)

        parsed = _json_object(_message_text(response))
        translations = parsed.get("translations")
        if not isinstance(translations, Mapping):
            raise AiRelayError("AI 特征翻译响应缺少 translations 对象")
        expected = {item["attribute_id"] for item in fields}
        if set(map(str, translations)) != expected:
            raise AiRelayError("AI 特征翻译返回的属性 ID 与请求不一致")
        result: dict[str, str] = {}
        for attribute_id in expected:
            value = str(translations.get(attribute_id) or "").strip()
            if not value or re.search(r"[\u3400-\u9fff]", value):
                raise AiRelayError(
                    f"AI 特征翻译未产生有效俄语值：ID {attribute_id}"
                )
            result[attribute_id] = value
        return result

    def generate_product_image(
        self,
        source_image_url: str,
        source_role: str | None,
        source: Mapping[str, Any],
        *,
        prompt_override: str | None = None,
    ) -> str:
        facts = {
            "title_zh": source.get("title_zh"),
            "attributes_zh": source.get("attributes_zh"),
            "source_role": source_role,
            "reference_image_url": source_image_url,
        }
        prompt = prompt_override or (
            "Создай реалистичное товарное изображение для OZON на основе фактов и "
            "референса ниже. Сохрани форму, цвет, комплектацию и конструкцию товара. "
            "Не добавляй логотипы и бренды. Удали китайский текст; если текст нужен, "
            "используй только естественный русский. Чистая композиция, без водяных "
            "знаков, пригодно для карточки маркетплейса. "
            + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        )
        response = self._post(
            "/images/generations",
            self.image_api_key,
            {
                "model": self.image_model,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1536",
                "quality": "high",
                "response_format": "url",
                "reference_image_url": source_image_url,
            },
            operation="图片生成",
            model=self.image_model,
        )
        return _image_url(response)

    def public_status(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "text_model": normalize_text_model(self.text_model),
            "image_model": self.image_model,
            "text_api_key_configured": bool(self.text_api_key),
            "image_api_key_configured": bool(self.image_api_key),
        }
