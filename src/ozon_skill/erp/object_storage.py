"""Aliyun OSS-backed persistent image hosting for Ozon product media."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from ozon_skill.content_generation.rich_content import validate_public_image_url


_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9/_-]{0,120}$")
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ObjectStorageError(RuntimeError):
    """A safe, user-facing object storage failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _endpoint(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not (host.endswith(".aliyuncs.com") and "oss-" in host)
    ):
        raise ObjectStorageError(
            "OSS Endpoint 必须是阿里云 HTTPS 地址，例如 https://oss-cn-hongkong.aliyuncs.com"
        )
    return f"https://{host}"


def _public_base(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise ObjectStorageError("公开访问/CDN 地址必须是 HTTPS URL")
    return raw


def _image_type(data: bytes, content_type: str = "") -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ObjectStorageError(
        f"图片内容不是受支持的 PNG/JPEG/WebP（Content-Type: {content_type or '未知'}）"
    )


def _reject_private_destination(url: str) -> None:
    host = urlparse(url).hostname or ""
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ObjectStorageError(f"图片域名无法解析：{host}") from exc
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ObjectStorageError("图片 URL 指向非公网地址，已阻止下载")


class AliyunOssClient:
    def __init__(self, config: Mapping[str, Any], *, timeout_seconds: float = 45.0) -> None:
        self.endpoint = _endpoint(config.get("endpoint"))
        self.bucket = str(config.get("bucket") or "").strip().lower()
        self.access_key_id = str(config.get("access_key_id") or "").strip()
        self._access_key_secret = str(config.get("access_key_secret") or "").strip()
        self.public_base_url = _public_base(config.get("public_base_url"))
        self.prefix = str(config.get("prefix") or "northstar").strip("/")
        if not _BUCKET.fullmatch(self.bucket):
            raise ObjectStorageError("OSS Bucket 名称格式无效")
        if not self.access_key_id or not self._access_key_secret:
            raise ObjectStorageError("OSS AccessKey ID 和 Secret 均不能为空")
        if not _PREFIX.fullmatch(self.prefix):
            raise ObjectStorageError("对象目录只能包含字母、数字、斜杠、下划线和连字符")
        self.timeout_seconds = timeout_seconds

    @property
    def _host(self) -> str:
        return f"{self.bucket}.{urlparse(self.endpoint).hostname}"

    def put(self, key: str, data: bytes, content_type: str) -> str:
        encoded_key = quote(key.lstrip("/"), safe="/-_.~")
        date = formatdate(usegmt=True)
        canonical_headers = "x-oss-object-acl:public-read\n"
        canonical_resource = f"/{self.bucket}/{encoded_key}"
        string_to_sign = (
            f"PUT\n\n{content_type}\n{date}\n"
            f"{canonical_headers}{canonical_resource}"
        )
        signature = base64.b64encode(
            hmac.new(
                self._access_key_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        request = Request(
            f"https://{self._host}/{encoded_key}",
            data=data,
            method="PUT",
            headers={
                "Authorization": f"OSS {self.access_key_id}:{signature}",
                "Content-Type": content_type,
                "Date": date,
                "Host": self._host,
                "User-Agent": "northstar-erp-object-storage/1.0",
                "x-oss-object-acl": "public-read",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status not in {200, 201}:
                    raise ObjectStorageError(f"OSS 上传返回 HTTP {response.status}")
        except HTTPError as exc:
            preview = exc.read(500).decode("utf-8", errors="replace")
            raise ObjectStorageError(
                f"OSS 上传失败：HTTP {exc.code}；{preview[:300]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ObjectStorageError(f"OSS 网络请求失败：{exc}") from exc
        public_url = f"{self.public_base_url}/{encoded_key}"
        validate_public_image_url(public_url)
        return public_url

    def mirror(self, source_url: str, pipeline_id: str) -> dict[str, Any]:
        validate_public_image_url(source_url)
        _reject_private_destination(source_url)
        request = Request(
            source_url,
            headers={"User-Agent": "northstar-erp-image-mirror/1.0", "Accept": "image/*"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(_MAX_IMAGE_BYTES + 1)
        except HTTPError as exc:
            raise ObjectStorageError(f"下载待托管图片失败：HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ObjectStorageError(f"下载待托管图片失败：{exc}") from exc
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ObjectStorageError("待托管图片为空或超过 20 MiB")
        normalized_type, suffix = _image_type(data, content_type)
        digest = hashlib.sha256(data).hexdigest()
        safe_pipeline = re.sub(r"[^A-Za-z0-9._-]", "_", pipeline_id)[:80]
        key = "/".join(
            part for part in (self.prefix, safe_pipeline, f"{digest}.{suffix}") if part
        )
        return {
            "public_url": self.put(key, data, normalized_type),
            "object_key": key,
            "sha256": digest,
            "content_type": normalized_type,
            "size_bytes": len(data),
        }

    def verify_public_image(self, public_url: str) -> None:
        validate_public_image_url(public_url)
        _reject_private_destination(public_url)
        request = Request(
            public_url,
            headers={"User-Agent": "northstar-erp-public-image-check/1.0", "Accept": "image/*"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise ObjectStorageError(
                f"验证图片无法公开访问：HTTP {exc.code}；请检查 Bucket 公共访问策略或 CDN 回源"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ObjectStorageError(f"验证图片无法公开访问：{exc}") from exc
        if not data or len(data) > 2 * 1024 * 1024:
            raise ObjectStorageError("验证图片公开响应为空或异常过大")
        _image_type(data, content_type)


class ObjectStorageRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ObjectStorageError("图片存储配置文件无法读取") from exc
        if not isinstance(value, dict):
            raise ObjectStorageError("图片存储配置文件格式无效")
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
    def _public(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "provider": "aliyun_oss",
            "configured": bool(value),
            "endpoint": value.get("endpoint"),
            "bucket": value.get("bucket"),
            "public_base_url": value.get("public_base_url"),
            "prefix": value.get("prefix"),
            "access_key_id_hint": (
                f"***{str(value.get('access_key_id') or '')[-4:]}"
                if value.get("access_key_id")
                else None
            ),
            "verified_at": value.get("verified_at"),
            "updated_at": value.get("updated_at"),
        }

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            return self._public(self._read())

    def save(self, value: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._read()
            secret = str(value.get("access_key_secret") or "").strip() or str(
                existing.get("access_key_secret") or ""
            )
            record = {
                "endpoint": _endpoint(value.get("endpoint") or existing.get("endpoint")),
                "bucket": str(value.get("bucket") or existing.get("bucket") or "").strip().lower(),
                "public_base_url": _public_base(
                    value.get("public_base_url") or existing.get("public_base_url")
                ),
                "prefix": str(value.get("prefix") or existing.get("prefix") or "northstar").strip("/"),
                "access_key_id": str(value.get("access_key_id") or existing.get("access_key_id") or "").strip(),
                "access_key_secret": secret,
                "updated_at": _now(),
            }
            client = AliyunOssClient(record)
            probe = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            probe_url = client.put(
                "/".join(part for part in (client.prefix, "health", "verify.png") if part),
                probe,
                "image/png",
            )
            client.verify_public_image(probe_url)
            record["verified_at"] = _now()
            self._write(record)
            return self._public(record)

    def client(self) -> AliyunOssClient | None:
        with self._lock:
            value = self._read()
        return AliyunOssClient(value) if value else None

    def delete(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
