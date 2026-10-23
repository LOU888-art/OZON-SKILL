from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.object_storage import (
    AliyunOssClient,
    ObjectStorageError,
    ObjectStorageRegistry,
)
from ozon_skill.erp.service import PipelineService


CONFIG = {
    "endpoint": "https://oss-cn-hongkong.aliyuncs.com",
    "bucket": "northstar-images",
    "public_base_url": "https://cdn.example.com/assets",
    "prefix": "northstar",
    "access_key_id": "test-access-id",
    "access_key_secret": "test-secret",
}


class _Response:
    status = 200
    headers = {"Content-Type": "image/png"}

    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.data


class ObjectStorageTests(unittest.TestCase):
    def test_put_uses_oss_signature_without_exposing_secret(self) -> None:
        client = AliyunOssClient(CONFIG)
        with patch("ozon_skill.erp.object_storage.urlopen", return_value=_Response()) as mocked:
            result = client.put("northstar/item/image.png", b"png", "image/png")

        request = mocked.call_args.args[0]
        self.assertEqual(result, "https://cdn.example.com/assets/northstar/item/image.png")
        self.assertEqual(request.full_url, "https://northstar-images.oss-cn-hongkong.aliyuncs.com/northstar/item/image.png")
        self.assertTrue(request.get_header("Authorization").startswith("OSS test-access-id:"))
        self.assertNotIn("test-secret", str(request.header_items()))

    def test_delete_is_signed_and_restricted_to_configured_prefix(self) -> None:
        client = AliyunOssClient(CONFIG)
        with patch("ozon_skill.erp.object_storage.urlopen", return_value=_Response()) as mocked:
            client.delete("northstar/item/image.png")

        request = mocked.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(
            request.full_url,
            "https://northstar-images.oss-cn-hongkong.aliyuncs.com/northstar/item/image.png",
        )
        self.assertTrue(request.get_header("Authorization").startswith("OSS test-access-id:"))
        self.assertNotIn("test-secret", str(request.header_items()))
        with self.assertRaisesRegex(ObjectStorageError, "Northstar"):
            client.delete("another-prefix/image.png")

    def test_registry_hides_secret_and_uses_private_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".object-storage.json"
            registry = ObjectStorageRegistry(path)
            with (
                patch.object(AliyunOssClient, "put", return_value="https://cdn.example.com/verify.png"),
                patch.object(AliyunOssClient, "verify_public_image"),
            ):
                public = registry.save(CONFIG)

            self.assertTrue(public["configured"])
            self.assertEqual(public["access_key_id_hint"], "***s-id")
            self.assertNotIn("access_key_secret", public)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_mirror_hashes_image_and_returns_cdn_url(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        client = AliyunOssClient(CONFIG)
        with (
            patch("ozon_skill.erp.object_storage.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
            patch("ozon_skill.erp.object_storage.urlopen", return_value=_Response(png)),
            patch.object(client, "put", return_value="https://cdn.example.com/assets/northstar/item/hash.png") as put,
        ):
            result = client.mirror("https://images.example.com/source.png", "item")

        self.assertEqual(result["content_type"], "image/png")
        self.assertEqual(result["size_bytes"], len(png))
        self.assertTrue(result["object_key"].endswith(".png"))
        put.assert_called_once()

    def test_private_source_address_is_blocked(self) -> None:
        client = AliyunOssClient(CONFIG)
        with patch("ozon_skill.erp.object_storage.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(ObjectStorageError, "非公网地址"):
                client.mirror("https://images.example.com/source.png", "item")

    def test_public_verification_checks_image_bytes(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"public"
        client = AliyunOssClient(CONFIG)
        with (
            patch("ozon_skill.erp.object_storage.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
            patch("ozon_skill.erp.object_storage.urlopen", return_value=_Response(png)),
        ):
            client.verify_public_image("https://cdn.example.com/assets/verify.png")

    def test_pipeline_mirrors_duplicate_urls_only_once(self) -> None:
        class _Client:
            public_base_url = "https://cdn.example.com/assets"
            bucket = "northstar-assets"
            endpoint = "oss-cn-hongkong.aliyuncs.com"

            def __init__(self) -> None:
                self.calls = 0

            def mirror(self, source_url: str, pipeline_id: str):
                self.calls += 1
                self.assertions = (source_url, pipeline_id)
                return {
                    "public_url": "https://cdn.example.com/assets/northstar/item/hash.png",
                    "object_key": "northstar/item/hash.png",
                    "sha256": "abc",
                    "size_bytes": 123,
                }

        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            client = _Client()
            with patch.object(service.object_storage, "client", return_value=client):
                rows = service._mirror_generated_images(
                    "item",
                    [
                        {"final_public_url": "https://images.example.com/source.png"},
                        {"final_public_url": "https://images.example.com/source.png"},
                    ],
                )

        self.assertEqual(client.calls, 1)
        self.assertEqual(client.assertions, ("https://images.example.com/source.png", "item"))
        self.assertTrue(all(row["storage_status"] == "MIRRORED" for row in rows))
        self.assertTrue(all(row["storage_sha256"] == "abc" for row in rows))

    def test_pipeline_blocks_failed_storage_url_before_ozon(self) -> None:
        class _FailingClient:
            public_base_url = "https://cdn.example.com/assets"

            def mirror(self, _source_url: str, _pipeline_id: str):
                raise ObjectStorageError("Bucket 不允许写入")

        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with patch.object(
                service.object_storage, "client", return_value=_FailingClient()
            ):
                rows = service._mirror_generated_images(
                    "item", [{"final_public_url": "https://images.example.com/source.png"}]
                )

        self.assertIsNone(rows[0]["final_public_url"])
        self.assertEqual(rows[0]["storage_status"], "FAILED")
        self.assertIn("Bucket 不允许写入", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
