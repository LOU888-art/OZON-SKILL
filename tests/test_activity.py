from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.activity import ActivityRegistry


class ActivityRegistryTests(unittest.TestCase):
    def test_records_recent_operations_without_request_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".activity.jsonl"
            registry = ActivityRegistry(path)

            first = registry.append(
                actor="QSM",
                method="POST",
                path="/api/pipelines",
                status=201,
                remote_address="203.0.113.10",
            )
            second = registry.append(
                actor="QSM",
                method="POST",
                path="/api/pipelines/123/stages/5",
                status=400,
                remote_address="203.0.113.10",
            )

            records = registry.list(100)
            self.assertEqual([item["activity_id"] for item in records], [second["activity_id"], first["activity_id"]])
            self.assertEqual(records[0]["outcome"], "FAILED")
            self.assertEqual(records[1]["outcome"], "SUCCESS")
            self.assertNotIn("body", records[0])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_limit_is_enforced_and_corrupt_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".activity.jsonl"
            registry = ActivityRegistry(path)
            for index in range(4):
                registry.append(
                    actor="admin",
                    method="DELETE",
                    path=f"/api/stores/{index}",
                    status=200,
                )
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")

            records = registry.list(3)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["path"], "/api/stores/3")
            self.assertEqual(records[1]["path"], "/api/stores/2")

    def test_empty_registry_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = ActivityRegistry(Path(temporary) / "missing.jsonl")
            self.assertEqual(registry.list(), [])


if __name__ == "__main__":
    unittest.main()
