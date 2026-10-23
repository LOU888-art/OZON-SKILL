from __future__ import annotations

import io
import unittest
import zipfile

from ozon_skill.erp.app import _PLUGIN_FILES, _research_plugin_archive


class ResearchPluginDownloadTests(unittest.TestCase):
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
