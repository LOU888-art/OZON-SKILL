"""Private external alert channels with deduplicated delivery history."""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import tempfile
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


class AlertChannelError(RuntimeError):
    """User-facing external alert configuration or delivery error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_wecom_sender(config: Mapping[str, Any], title: str, body: str) -> None:
    payload = json.dumps(
        {"msgtype": "text", "text": {"content": f"{title}\n{body}"}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        str(config["wecom_webhook_url"]),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        result = json.loads(response.read(64 * 1024).decode("utf-8"))
    if not isinstance(result, Mapping) or int(result.get("errcode") or 0) != 0:
        raise AlertChannelError(f"企业微信返回失败：{result}")


def _default_email_sender(config: Mapping[str, Any], title: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = str(config["email_from"])
    message["To"] = ", ".join(config["email_recipients"])
    message.set_content(body)
    host = str(config["smtp_host"])
    port = int(config["smtp_port"])
    security = str(config.get("smtp_security") or "starttls")
    context = ssl.create_default_context()
    client: smtplib.SMTP
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=10)
    try:
        if security == "starttls":
            client.starttls(context=context)
        username = str(config.get("smtp_username") or "")
        password = str(config.get("smtp_password") or "")
        if username:
            client.login(username, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            client.close()


class AlertChannelRegistry:
    def __init__(
        self,
        path: Path,
        *,
        wecom_sender: Callable[[Mapping[str, Any], str, str], None] = _default_wecom_sender,
        email_sender: Callable[[Mapping[str, Any], str, str], None] = _default_email_sender,
    ) -> None:
        self.path = path.resolve()
        self.wecom_sender = wecom_sender
        self.email_sender = email_sender
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "config": {}, "deliveries": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AlertChannelError("外部通知配置文件无法读取") from exc
        if not isinstance(value, dict):
            raise AlertChannelError("外部通知配置文件格式无效")
        value.setdefault("config", {})
        value.setdefault("deliveries", [])
        if not isinstance(value["config"], dict) or not isinstance(value["deliveries"], list):
            raise AlertChannelError("外部通知配置文件格式无效")
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
    def _public(document: Mapping[str, Any]) -> dict[str, Any]:
        config = document.get("config") or {}
        deliveries = document.get("deliveries") or []
        return {
            "enabled": bool(config.get("enabled")),
            "wecom_enabled": bool(config.get("wecom_enabled")),
            "wecom_configured": bool(config.get("wecom_webhook_url")),
            "email_enabled": bool(config.get("email_enabled")),
            "email_configured": bool(config.get("smtp_host") and config.get("email_recipients")),
            "smtp_host": config.get("smtp_host", ""),
            "smtp_port": config.get("smtp_port", 587),
            "smtp_security": config.get("smtp_security", "starttls"),
            "smtp_username": config.get("smtp_username", ""),
            "smtp_password_configured": bool(config.get("smtp_password")),
            "email_from": config.get("email_from", ""),
            "email_recipients": list(config.get("email_recipients") or []),
            "deliveries": [
                {
                    key: item.get(key)
                    for key in ("event_id", "channel", "status", "error", "created_at", "test")
                }
                for item in list(deliveries)[-50:][::-1]
                if isinstance(item, Mapping)
            ],
        }

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            return self._public(self._read())

    @staticmethod
    def _email(value: Any, field: str) -> str:
        address = str(value or "").strip()
        if not address or parseaddr(address)[1] != address or "@" not in address:
            raise AlertChannelError(f"{field} 不是有效邮箱地址")
        return address

    def save(self, value: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            existing = document["config"]
            webhook = str(value.get("wecom_webhook_url") or "").strip() or existing.get("wecom_webhook_url", "")
            if webhook:
                parsed = urlparse(webhook)
                query = parse_qs(parsed.query)
                if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com" or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str((query.get("key") or [""])[0])):
                    raise AlertChannelError("企业微信地址必须是 qyapi.weixin.qq.com 的 HTTPS 机器人 Webhook")
            recipients_value = value.get("email_recipients", existing.get("email_recipients", []))
            if isinstance(recipients_value, str):
                recipients_value = [item.strip() for item in re.split(r"[,;\n]", recipients_value) if item.strip()]
            if not isinstance(recipients_value, list) or len(recipients_value) > 20:
                raise AlertChannelError("邮件收件人必须是最多 20 个邮箱")
            recipients = [self._email(item, "收件人") for item in recipients_value]
            host = str(value.get("smtp_host") or existing.get("smtp_host") or "").strip()
            if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                raise AlertChannelError("SMTP 主机名格式无效")
            try:
                port = int(value.get("smtp_port") or existing.get("smtp_port") or 587)
            except (TypeError, ValueError) as exc:
                raise AlertChannelError("SMTP 端口无效") from exc
            if port < 1 or port > 65535:
                raise AlertChannelError("SMTP 端口无效")
            security = str(value.get("smtp_security") or existing.get("smtp_security") or "starttls")
            if security not in {"starttls", "ssl", "none"}:
                raise AlertChannelError("SMTP 安全方式无效")
            email_from_raw = value.get("email_from") or existing.get("email_from") or ""
            email_from = self._email(email_from_raw, "发件人") if email_from_raw else ""
            password = str(value.get("smtp_password") or "").strip() or existing.get("smtp_password", "")
            config = {
                "enabled": value.get("enabled") is True,
                "wecom_enabled": value.get("wecom_enabled") is True,
                "wecom_webhook_url": webhook,
                "email_enabled": value.get("email_enabled") is True,
                "smtp_host": host,
                "smtp_port": port,
                "smtp_security": security,
                "smtp_username": str(value.get("smtp_username") or existing.get("smtp_username") or "").strip(),
                "smtp_password": password,
                "email_from": email_from,
                "email_recipients": recipients,
            }
            if config["wecom_enabled"] and not webhook:
                raise AlertChannelError("启用企业微信前必须填写机器人 Webhook")
            if config["email_enabled"] and (not host or not email_from or not recipients):
                raise AlertChannelError("启用邮件前必须填写 SMTP、发件人和收件人")
            document["config"] = config
            self._write(document)
            return self._public(document)

    def delete(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def send(self, title: str, body: str, *, event_id: str, test: bool = False) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            config = dict(document["config"])
            if not test and any(item.get("event_id") == event_id for item in document["deliveries"] if isinstance(item, Mapping)):
                return {"status": "DEDUPLICATED", "results": []}
            if not config.get("enabled"):
                raise AlertChannelError("外部通知总开关尚未启用")
            channels: list[tuple[str, Callable[[Mapping[str, Any], str, str], None]]] = []
            if config.get("wecom_enabled"):
                channels.append(("WECOM", self.wecom_sender))
            if config.get("email_enabled"):
                channels.append(("EMAIL", self.email_sender))
            if not channels:
                raise AlertChannelError("至少启用一个外部通知渠道")
            results: list[dict[str, Any]] = []
            for channel, sender in channels:
                try:
                    sender(config, str(title)[:160], str(body)[:4000])
                    results.append({"event_id": event_id, "channel": channel, "status": "SENT", "error": "", "created_at": _now(), "test": test})
                except Exception as exc:
                    results.append({"event_id": event_id, "channel": channel, "status": "FAILED", "error": str(exc)[:500], "created_at": _now(), "test": test})
            document["deliveries"] = [*document["deliveries"], *results][-500:]
            self._write(document)
            return {"status": "SENT" if all(item["status"] == "SENT" for item in results) else "PARTIAL", "results": results}
