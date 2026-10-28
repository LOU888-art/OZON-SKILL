from __future__ import annotations

import io
import unittest
import zipfile

from ozon_skill.erp.app import (
    _COLLECTOR_PLUGIN_FILES,
    _PLUGIN_FILES,
    _collector_1688_plugin_archive,
    _research_plugin_archive,
)


class ResearchPluginDownloadTests(unittest.TestCase):
    def test_1688_collector_archive_is_installable(self) -> None:
        body = _collector_1688_plugin_archive()
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    f"northstar-1688-collector/{name}"
                    for name in _COLLECTOR_PLUGIN_FILES
                },
            )
            manifest = archive.read(
                "northstar-1688-collector/manifest.json"
            ).decode("utf-8")
            self.assertIn(
                "northstar-1688-collector/main-probe.js", archive.namelist()
            )
        self.assertIn('"manifest_version": 3', manifest)
        self.assertIn('"version": "0.2.0"', manifest)
        self.assertIn('"https://*.1688.com/*"', manifest)

    def test_archive_contains_only_installable_extension_files(self) -> None:
        body = _research_plugin_archive()
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {f"northstar-ozon-research/{name}" for name in _PLUGIN_FILES},
            )
            manifest = archive.read(
                "northstar-ozon-research/manifest.json"
            ).decode("utf-8")
        self.assertIn('"manifest_version": 3', manifest)


if __name__ == "__main__":
    unittest.main()
