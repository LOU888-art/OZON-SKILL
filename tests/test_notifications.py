from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.notifications import NotificationRegistry


class NotificationRegistryTests(unittest.TestCase):
    def test_read_state_is_isolated_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".notifications.json"
            registry = NotificationRegistry(path)
            items = [
                {"notification_id": "first", "title": "First"},
                {"notification_id": "second", "title": "Second"},
            ]

            registry.mark_read("QSM", ["first"])

            owner = registry.decorate("qsm", items)
            operator = registry.decorate("operator", items)
            self.assertEqual([item["read"] for item in owner], [True, False])
            self.assertEqual([item["read"] for item in operator], [False, False])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_registry_keeps_only_recent_read_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = NotificationRegistry(Path(temporary) / "state.json")
            registry.mark_read("QSM", [f"notification-{index}" for index in range(1100)])

            old_and_new = registry.decorate(
                "QSM",
                [
                    {"notification_id": "notification-0"},
                    {"notification_id": "notification-1099"},
                ],
            )
            self.assertEqual([item["read"] for item in old_and_new], [False, True])

    def test_corrupt_state_recovers_without_blocking_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            registry = NotificationRegistry(path)

            decorated = registry.decorate(
                "QSM", [{"notification_id": "failure", "title": "Failure"}]
            )
            self.assertFalse(decorated[0]["read"])


if __name__ == "__main__":
    unittest.main()
