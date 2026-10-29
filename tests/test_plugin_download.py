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
            popup = archive.read(
                "northstar-1688-collector/popup.js"
            ).decode("utf-8")
            content = archive.read(
                "northstar-1688-collector/content.js"
            ).decode("utf-8")
            probe = archive.read(
                "northstar-1688-collector/main-probe.js"
            ).decode("utf-8")
        self.assertIn('"manifest_version": 3', manifest)
        self.assertIn('"version": "0.3.0"', manifest)
        self.assertIn('"https://*.1688.com/*"', manifest)
        self.assertIn('chrome.storage.local.set({ erpUrl: origin })', popup)
        self.assertIn('chrome.storage.local.get("erpUrl")', popup)
        self.assertIn("scheduleErpUrlSave", popup)
        self.assertIn('"context", "__INIT_DATA__"', probe)
        self.assertIn("mapKeyPattern.test(key) && Array.isArray(value)", content)
        self.assertNotIn("stock_quantity: 999", content)

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
        self.assertIn('"version": "0.2.0"', manifest)
        self.assertIn('"https://www.ozon.ru/*"', manifest)
        self.assertIn(
            "northstar-ozon-research/ozon-overlay.js", archive.namelist()
        )
        self.assertIn(
            "northstar-ozon-research/ozon-overlay.css", archive.namelist()
        )


if __name__ == "__main__":
    unittest.main()
