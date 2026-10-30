from __future__ import annotations

import unittest
from unittest.mock import patch

from ozon_skill.ai_relay.client import (
    AiRelayClient,
    AiRelayError,
    relay_timeout_seconds,
)


class _JsonResponse:
    status = 200

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok":true}'


class AiRelayResilienceTests(unittest.TestCase):
    def _client(self) -> AiRelayClient:
        return AiRelayClient(
            base_url="https://relay.example.com/v1",
            text_api_key="text-secret",
            image_api_key="image-secret",
            timeout_seconds=60,
        )

    def test_post_retries_read_timeouts_before_succeeding(self) -> None:
        with (
            patch(
                "ozon_skill.ai_relay.client.urlopen",
                side_effect=[
                    TimeoutError("The read operation timed out"),
                    TimeoutError("The read operation timed out"),
                    _JsonResponse(),
                ],
            ) as opener,
            patch("ozon_skill.ai_relay.client.time.sleep") as sleeper,
        ):
            result = self._client()._post(
                "/chat/completions",
                "text-secret",
                {"model": "test"},
                operation="文本生成",
                model="test",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_exhausted_timeout_is_reported_in_chinese(self) -> None:
        with (
            patch(
                "ozon_skill.ai_relay.client.urlopen",
                side_effect=TimeoutError("The read operation timed out"),
            ),
            patch("ozon_skill.ai_relay.client.time.sleep"),
        ):
            with self.assertRaisesRegex(
                AiRelayError,
                "读取超时.*已自动重试 3 次",
            ):
                self._client()._post(
                    "/images/generations",
                    "image-secret",
                    {"model": "test"},
                    operation="图片生成",
                    model="test",
                )

    def test_environment_timeout_is_bounded(self) -> None:
        with patch.dict("os.environ", {"AI_RELAY_TIMEOUT_SECONDS": "720"}):
            self.assertEqual(relay_timeout_seconds(), 720)
        with patch.dict("os.environ", {"AI_RELAY_TIMEOUT_SECONDS": "9999"}):
            self.assertEqual(relay_timeout_seconds(), 900)
        with patch.dict("os.environ", {"AI_RELAY_TIMEOUT_SECONDS": "invalid"}):
            self.assertEqual(relay_timeout_seconds(), 360)


if __name__ == "__main__":
    unittest.main()
