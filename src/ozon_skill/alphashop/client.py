"""Authenticated AlphaShop calls without exposing credential values."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DETAIL_ENDPOINT = "https://api.alphashop.cn/alphashop.openclaw.offer.detail.query/1.0"
CATEGORY_ENDPOINT = "https://api.alphashop.cn/alphashop.openclaw.offer.cate.ozon.query/1.0"
IMAGE_TRANSLATE_ENDPOINT = "https://api.alphashop.cn/ai.image.translateImagePro/1.0"
INSUFFICIENT_POINTS_MESSAGE = (
    "AlphaShop 积分不足，请前往 "
    "https://www.alphashop.cn/seller-center/home/api-list 购买积分后重试"
)
_OFFER_PATH = re.compile(r"/offer/(\d+)\.html(?:$|[/?#])")
_INSUFFICIENT_MARKERS = (
    "欠费",
    "余额不足",
    "积分不足",
    "insufficient balance",
    "insufficient points",
    "balance not enough",
)


class AlphaShopApiError(RuntimeError):
    """AlphaShop returned an unusable API result."""


class AlphaShopInsufficientPointsError(AlphaShopApiError):
    """AlphaShop requires more points; callers must not retry."""


class AlphaShopHttpError(AlphaShopApiError):
    def __init__(self, status_code: int, response_preview: str) -> None:
        self.status_code = status_code
        self.response_preview = response_preview
        super().__init__(
            f"AlphaShop HTTP {status_code}，响应前 500 字符：{response_preview}"
        )


def extract_offer_id(value: str | int) -> str:
    """Accept a numeric offer ID or a detail.1688.com offer URL."""

    text = str(value).strip()
    if text.isdigit():
        return text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "1688.com" or host.endswith(".1688.com")
    ):
        raise ValueError("请输入纯数字商品 ID 或有效的 1688 商品链接")
    match = _OFFER_PATH.search(parsed.path)
    if not match:
        raise ValueError("1688 链接中未找到 /offer/<id>.html")
    return match.group(1)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def build_jwt(access_key: str, secret_key: str, *, now: int | None = None) -> tuple[str, dict[str, Any]]:
    """Build the exact HS256 JWT required by AlphaShop."""

    if not access_key or not secret_key:
        raise ValueError("AlphaShop credentials must not be empty")
    timestamp = int(time.time()) if now is None else int(now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": access_key, "exp": timestamp + 1800, "nbf": timestamp - 5}
    segments = [
        _base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{segments[0]}.{segments[1]}.{_base64url(signature)}", payload


def _contains_insufficient_points(text: str) -> bool:
    normalized = text.casefold()
    return any(marker in normalized for marker in _INSUFFICIENT_MARKERS)


def _looks_like_api_error(payload: Mapping[str, Any]) -> bool:
    if payload.get("success") is False:
        return True
    code = payload.get("code") or payload.get("errorCode") or payload.get("error_code")
    return code not in (None, 0, "0", 200, "200", "SUCCESS", "success")


def _translated_image_url(payload: Mapping[str, Any]) -> str | None:
    """Read AlphaShop image URLs across documented gateway wrapper variants."""

    url_keys = {
        "translatedimageurl",
        "translatedurl",
        "outputimageurl",
        "resultimageurl",
        "targetimageurl",
        "translatedimage",
        "outputimage",
        "resultimage",
        "targetimage",
        "fileurl",
        "downloadurl",
        "imageurl",
        "url",
    }
    container_keys = {"result", "data", "output", "images", "items"}

    def is_url(value: Any) -> bool:
        return isinstance(value, str) and value.strip().startswith(("https://", "http://"))

    def visit(value: Any, *, scalar_allowed: bool = False) -> str | None:
        if is_url(value) and scalar_allowed:
            return str(value).strip()
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in url_keys and is_url(nested):
                    return str(nested).strip()
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                found = visit(
                    nested,
                    scalar_allowed=scalar_allowed or normalized in container_keys,
                )
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = visit(nested, scalar_allowed=scalar_allowed)
                if found:
                    return found
        return None

    return visit(payload)


def _image_response_shape(payload: Mapping[str, Any]) -> str:
    keys: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                keys.add(str(key))
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return f"response_keys={sorted(keys)[:30]}"


def extract_external_category_pair(payload: Mapping[str, Any]) -> tuple[int, int]:
    """Extract ``description_category_id,type_id`` from a mapping response."""

    pending: list[Any] = [payload]
    while pending:
        value = pending.pop(0)
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).replace("_", "").casefold()
                if normalized == "externalcategoryid":
                    if isinstance(nested, str):
                        parts = [part.strip() for part in nested.split(",")]
                    elif isinstance(nested, (list, tuple)):
                        parts = [str(part).strip() for part in nested]
                    else:
                        parts = []
                    if len(parts) == 2 and all(part.isdigit() for part in parts):
                        return int(parts[0]), int(parts[1])
                pending.append(nested)
        elif isinstance(value, list):
            pending.extend(value)
    raise AlphaShopApiError(
        "AlphaShop 类目映射响应缺少有效 externalCategoryId"
    )


@dataclass(frozen=True)
class AlphaShopExchange:
    payload: dict[str, Any]
    response_preview: str
    jwt_payload_redacted: dict[str, Any]


@dataclass(frozen=True)
class AlphaShopClient:
    access_key: str
    secret_key: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "AlphaShopClient":
        access_key = os.environ.get("ALPHASHOP_ACCESS_KEY")
        secret_key = os.environ.get("ALPHASHOP_SECRET_KEY")
        if not access_key or not secret_key:
            raise AlphaShopApiError(
                "缺少环境变量：ALPHASHOP_ACCESS_KEY, ALPHASHOP_SECRET_KEY"
            )
        access_key = access_key.strip()
        secret_key = secret_key.strip()
        if not access_key or not secret_key:
            raise AlphaShopApiError(
                "环境变量 ALPHASHOP_ACCESS_KEY 和 ALPHASHOP_SECRET_KEY 不能为空白"
            )
        return cls(access_key, secret_key)

    def offer_detail(self, offer_id_or_url: str | int) -> dict[str, Any]:
        return self.offer_detail_exchange(offer_id_or_url).payload

    def offer_detail_exchange(self, offer_id_or_url: str | int) -> AlphaShopExchange:
        offer_id = extract_offer_id(offer_id_or_url)
        return self._post(DETAIL_ENDPOINT, {"productId": offer_id})

    def category_mapping(self, category_id: str | int) -> AlphaShopExchange:
        value = str(category_id).strip()
        if not value.isdigit():
            raise ValueError("AlphaShop categoryId 必须是数字")
        return self._post(CATEGORY_ENDPOINT, {"categoryId": value})

    def translate_image(self, image_url: str) -> tuple[str, AlphaShopExchange]:
        exchange = self._post(
            IMAGE_TRANSLATE_ENDPOINT,
            {"imageUrl": image_url, "sourceLanguage": "auto", "targetLanguage": "ru"},
            timeout_seconds=max(self.timeout_seconds, 120.0),
        )
        translated = _translated_image_url(exchange.payload)
        if not translated:
            raise AlphaShopApiError(
                "AlphaShop 图片翻译响应缺少可识别的公开图片 URL；"
                + _image_response_shape(exchange.payload)
            )
        return translated, exchange

    def _post(
        self,
        endpoint: str,
        body: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> AlphaShopExchange:
        token, jwt_payload = build_jwt(self.access_key, self.secret_key)
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ozon-skill-alphashop/1.0",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read(4096).decode("utf-8", errors="replace")
            preview = raw[:500]
            if _contains_insufficient_points(preview):
                raise AlphaShopInsufficientPointsError(INSUFFICIENT_POINTS_MESSAGE) from exc
            raise AlphaShopHttpError(exc.code, preview) from exc
        except URLError as exc:
            raise AlphaShopApiError(f"AlphaShop 请求失败：{exc.reason}") from exc
        preview = raw[:500]
        if status != 200:
            if _contains_insufficient_points(preview):
                raise AlphaShopInsufficientPointsError(INSUFFICIENT_POINTS_MESSAGE)
            raise AlphaShopHttpError(status, preview)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AlphaShopApiError("AlphaShop 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise AlphaShopApiError("AlphaShop 返回的 JSON 顶层不是对象")
        canonical = json.dumps(payload, ensure_ascii=False)
        if _contains_insufficient_points(canonical):
            raise AlphaShopInsufficientPointsError(INSUFFICIENT_POINTS_MESSAGE)
        if _looks_like_api_error(payload):
            message = payload.get("message") or payload.get("msg") or payload.get("errorMessage")
            raise AlphaShopApiError(f"AlphaShop API 返回错误：{str(message or payload)[:500]}")
        return AlphaShopExchange(
            payload=payload,
            response_preview=preview,
            jwt_payload_redacted={
                "iss": "***REDACTED***",
                "exp": jwt_payload["exp"],
                "nbf": jwt_payload["nbf"],
            },
        )
