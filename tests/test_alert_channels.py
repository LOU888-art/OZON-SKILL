from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.alert_channels import AlertChannelError, AlertChannelRegistry


class AlertChannelTests(unittest.TestCase):
    def test_secrets_are_not_returned_and_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".alert-channels.json"
            registry = AlertChannelRegistry(path)
            status = registry.save(
                {
                    "enabled": True,
                    "wecom_enabled": True,
                    "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdefghijklmnop",
                    "email_enabled": True,
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 465,
                    "smtp_security": "ssl",
                    "smtp_username": "alerts@example.com",
                    "smtp_password": "super-secret",
                    "email_from": "alerts@example.com",
                    "email_recipients": ["owner@example.com"],
                }
            )
            stored = path.read_text(encoding="utf-8")
            mode = os.stat(path).st_mode & 0o777

        self.assertNotIn("wecom_webhook_url", status)
        self.assertNotIn("smtp_password", status)
        self.assertTrue(status["wecom_configured"])
        self.assertTrue(status["smtp_password_configured"])
        self.assertIn("super-secret", stored)
        self.assertEqual(mode, 0o600)

    def test_delivery_is_deduplicated_and_history_excludes_message_body(self) -> None:
        sent: list[tuple[str, str]] = []

        def sender(config: dict, title: str, body: str) -> None:
            sent.append((title, body))

        with tempfile.TemporaryDirectory() as temporary:
            registry = AlertChannelRegistry(
                Path(temporary) / ".alerts.json", wecom_sender=sender
            )
            registry.save(
                {
                    "enabled": True,
                    "wecom_enabled": True,
                    "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdefghijklmnop",
                    "email_enabled": False,
                }
            )
            first = registry.send(
                "Failure", "sensitive product summary", event_id="event-1"
            )
            second = registry.send(
                "Failure", "sensitive product summary", event_id="event-1"
            )
            stored = json.loads(registry.path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "DEDUPLICATED")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("sensitive product summary", json.dumps(stored))

    def test_invalid_or_private_wecom_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = AlertChannelRegistry(Path(temporary) / ".alerts.json")
            with self.assertRaises(AlertChannelError):
                registry.save(
                    {
                        "enabled": True,
                        "wecom_enabled": True,
                        "wecom_webhook_url": "https://127.0.0.1/webhook?key=abcdefghijklmnop",
                    }
                )

    def test_disabled_registry_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = AlertChannelRegistry(Path(temporary) / ".alerts.json")
            registry.save({"enabled": False})
            with self.assertRaises(AlertChannelError):
                registry.send("Test", "Body", event_id="test", test=True)


if __name__ == "__main__":
    unittest.main()
