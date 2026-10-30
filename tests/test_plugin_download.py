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
    def test_unified_collector_archive_is_installable(self) -> None:
        body = _collector_1688_plugin_archive()
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    f"northstar-commerce-assistant/{name}"
                    for name in _COLLECTOR_PLUGIN_FILES
                },
            )
            manifest = archive.read(
                "northstar-commerce-assistant/manifest.json"
            ).decode("utf-8")
            self.assertIn(
                "northstar-commerce-assistant/collector-main-probe.js",
                archive.namelist(),
            )
            popup = archive.read(
                "northstar-commerce-assistant/popup.js"
            ).decode("utf-8")
            content = archive.read(
                "northstar-commerce-assistant/collector-content.js"
            ).decode("utf-8")
            probe = archive.read(
                "northstar-commerce-assistant/collector-main-probe.js"
            ).decode("utf-8")
        self.assertIn('"manifest_version": 3', manifest)
        self.assertIn('"version": "1.5.5"', manifest)
        self.assertIn('"cookies"', manifest)
        self.assertIn('"https://*.1688.com/*"', manifest)
        self.assertIn('"https://www.ozon.ru/*"', manifest)
        self.assertIn('ERP_ORIGIN = "https://8.217.125.205"', popup)
        self.assertIn("syncSeller", popup)
        self.assertIn('"context", "__INIT_DATA__"', probe)
        self.assertIn("mapKeyPattern.test(key) && Array.isArray(value)", content)
        self.assertIn("enrichSkuCommerce(skuList, priceRanges)", content)
        self.assertIn('collector_version: "0.3.5"', content)
        self.assertIn("highResolutionImageUrl", content)
        self.assertIn("axisValueImage", content)
        self.assertIn("const pageGroups = domSkuGroups()", content)
        self.assertIn("没有绑定页面规格名称和规格图片", content)
        self.assertIn("skuSpecificationsMatchGroups", content)
        self.assertIn("pageGroups.length === 1", content)
        self.assertIn("visibleSkuOptionImages", content)
        self.assertIn("overridden_sku_image_count", content)
        self.assertNotIn("stock_quantity: 999", content)

    def test_research_download_returns_the_same_unified_plugin(self) -> None:
        body = _research_plugin_archive()
        self.assertTrue(body.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    f"northstar-commerce-assistant/{name}"
                    for name in _PLUGIN_FILES
                },
            )
            manifest = archive.read(
                "northstar-commerce-assistant/manifest.json"
            ).decode("utf-8")
        self.assertIn('"manifest_version": 3', manifest)
        self.assertIn('"version": "1.5.5"', manifest)
        self.assertIn('"https://www.ozon.ru/*"', manifest)
        self.assertIn(
            "northstar-commerce-assistant/ozon-overlay.js", archive.namelist()
        )
        self.assertIn(
            "northstar-commerce-assistant/ozon-overlay.css", archive.namelist()
        )
        self.assertIn(
            "northstar-commerce-assistant/ozon-seller-content.js",
            archive.namelist(),
        )
        self.assertIn(
            "northstar-commerce-assistant/seller-metrics.js",
            archive.namelist(),
        )
        self.assertIn(
            "northstar-commerce-assistant/background.js",
            archive.namelist(),
        )
        self.assertIn(
            "northstar-commerce-assistant/collector-floating.css",
            archive.namelist(),
        )


if __name__ == "__main__":
    unittest.main()
