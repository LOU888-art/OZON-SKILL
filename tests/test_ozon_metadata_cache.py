from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ozon_skill.ozon.client import OzonDescriptionCategoryClient


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"result": [{"id": 1}]}).encode("utf-8")


class OzonMetadataCacheTests(unittest.TestCase):
    def test_repeated_metadata_request_uses_memory_cache_and_returns_a_copy(self) -> None:
        client = OzonDescriptionCategoryClient(
            "cache-test-client-20261029",
            "cache-test-secret-20261029",
        )
        with patch("ozon_skill.ozon.client.urlopen", return_value=_Response()) as request:
            first = client.category_tree(language="RU")
            first["result"].append({"id": 999})
            second = client.category_tree(language="RU")

        self.assertEqual(request.call_count, 1)
        self.assertEqual(second, {"result": [{"id": 1}]})


if __name__ == "__main__":
    unittest.main()
