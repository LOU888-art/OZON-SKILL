from __future__ import annotations

import unittest
from unittest.mock import patch

from ozon_skill.upload.client import OzonUploadApiError, OzonUploadClient


class OzonUploadClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OzonUploadClient(client_id="111", api_key="secret")

    def test_product_info_list_sends_only_one_identifier_family(self) -> None:
        with patch.object(
            OzonUploadClient, "_post", return_value={"items": []}
        ) as post:
            result = self.client.product_info_list(["QSM102801", "QSM102802"])

        self.assertEqual(result, {"items": []})
        path, body = post.call_args.args
        self.assertEqual(path, "/v3/product/info/list")
        self.assertEqual(body, {"offer_id": ["QSM102801", "QSM102802"]})

    def test_product_info_list_falls_back_to_v2_for_unavailable_v3(self) -> None:
        with patch.object(
            OzonUploadClient,
            "_post",
            side_effect=[
                OzonUploadApiError(
                    "OZON returned HTTP 501 for /v3/product/info/list: unknown service"
                ),
                {"result": {"items": []}},
            ],
        ) as post:
            result = self.client.product_info_list(["QSM102801"])

        self.assertEqual(result, {"result": {"items": []}})
        self.assertEqual(post.call_count, 2)
        fallback_path, fallback_body = post.call_args.args
        self.assertEqual(fallback_path, "/v2/product/info/list")
        self.assertEqual(
            fallback_body,
            {"offer_id": ["QSM102801"]},
        )

    def test_product_info_list_does_not_hide_authentication_errors(self) -> None:
        with patch.object(
            OzonUploadClient,
            "_post",
            side_effect=OzonUploadApiError("OZON returned HTTP 401"),
        ) as post:
            with self.assertRaisesRegex(OzonUploadApiError, "HTTP 401"):
                self.client.product_info_list(["QSM102801"])

        post.assert_called_once()

    def test_picture_import_uses_complete_v2_item_contract(self) -> None:
        with patch.object(
            OzonUploadClient, "_post", return_value={"result": {"task_id": 42}}
        ) as post:
            result = self.client.product_pictures_import(
                "QSM102801",
                ["https://cdn.example.com/1.png", "https://cdn.example.com/1.png"],
            )

        self.assertEqual(result, {"result": {"task_id": 42}})
        path, body = post.call_args.args
        self.assertEqual(path, "/v2/product/pictures/import")
        self.assertEqual(
            body,
            {
                "items": [
                    {
                        "offer_id": "QSM102801",
                        "images": ["https://cdn.example.com/1.png"],
                        "images360": [],
                        "color_image": "",
                    }
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
