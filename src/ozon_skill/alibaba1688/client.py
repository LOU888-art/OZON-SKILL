"""Minimal client for the official 1688 Open Platform gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class AlibabaApiError(RuntimeError):
    """Raised when the gateway cannot return a successful JSON response."""


def _parameter_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def generate_signature(api_path: str, params: Mapping[str, Any], app_secret: str) -> str:
    """Generate the uppercase HMAC-SHA1 signature required by param2 APIs.

    ``api_path`` does not include the ``/openapi/`` prefix. The signature field
    itself is never included in the signed parameter set.
    """

    if not app_secret:
        raise ValueError("app_secret must not be empty")
    pieces = [api_path]
    for key in sorted(params):
        if key == "_aop_signature" or params[key] is None:
            continue
        pieces.extend((key, _parameter_text(params[key])))
    message = "".join(pieces).encode("utf-8")
    digest = hmac.new(app_secret.encode("utf-8"), message, hashlib.sha1)
    return digest.hexdigest().upper()


@dataclass(frozen=True)
class Alibaba1688Client:
    """Official gateway client with a generic extension point for other APIs."""

    app_key: str
    app_secret: str
    access_token: str | None = None
    gateway: str = "https://gw.open.1688.com/openapi"
    timeout_seconds: float = 30.0

    PRODUCT_GET_API = "com.alibaba.product/alibaba.product.get"
    OFFER_PRICE_GET_API = "com.alibaba.offer/alibaba.offer.price.get"
    OFFER_SPEC_GET_API = "com.alibaba.offer/alibaba.offer.spec.get"

    def __post_init__(self) -> None:
        if not self.app_key:
            raise ValueError("app_key must not be empty")
        if not self.app_secret:
            raise ValueError("app_secret must not be empty")
        if not self.gateway.startswith("https://"):
            raise ValueError("gateway must use HTTPS")

    def product_get(self, offer_id: str | int) -> dict[str, Any]:
        """Fetch base product information with ``alibaba.product.get``."""

        if str(offer_id).strip() == "":
            raise ValueError("offer_id must not be empty")
        return self.call_api(
            self.PRODUCT_GET_API,
            {"productID": str(offer_id), "webSite": "1688"},
        )

    def call_api(
        self,
        api_name: str,
        params: Mapping[str, Any],
        *,
        version: int = 1,
    ) -> dict[str, Any]:
        """Call any param2 API.

        This is the extension point for ``alibaba.offer.price.get`` and
        ``alibaba.offer.spec.get`` once their responses are enabled for the
        application's 1688 API permissions.
        """

        api_path = f"param2/{version}/{api_name}/{self.app_key}"
        request_params: dict[str, Any] = {
            key: _parameter_text(value)
            for key, value in params.items()
            if value is not None
        }
        if self.access_token:
            request_params["access_token"] = self.access_token
        request_params["_aop_timestamp"] = str(int(time.time() * 1000))
        request_params["_aop_signature"] = generate_signature(
            api_path, request_params, self.app_secret
        )

        body = urlencode(request_params).encode("utf-8")
        request = Request(
            f"{self.gateway.rstrip('/')}/{api_path}",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "ozon-skill-1688-collector/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            # Do not include request parameters because they may contain a token.
            raise AlibabaApiError(f"1688 gateway returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise AlibabaApiError(f"1688 gateway request failed: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AlibabaApiError("1688 gateway returned non-JSON content") from exc
        if not isinstance(payload, dict):
            raise AlibabaApiError("1688 gateway returned an unexpected JSON shape")
        self._raise_for_api_error(payload)
        return payload

    @staticmethod
    def _raise_for_api_error(payload: Mapping[str, Any]) -> None:
        error_code = payload.get("errorCode") or payload.get("error_code")
        if error_code is None and isinstance(payload.get("error"), Mapping):
            error_code = payload["error"].get("code")
        if error_code is None:
            return
        message = (
            payload.get("errorMessage")
            or payload.get("error_message")
            or payload.get("message")
            or "unknown API error"
        )
        raise AlibabaApiError(f"1688 API error {error_code}: {message}")

