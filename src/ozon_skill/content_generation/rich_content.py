"""Builders and validators for OZON Rich-Контент 0.3."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


BANNED_IMAGE_HOSTS = {"imgur.com", "i.imgur.com", "litterbox.catbox.moe", "uguu.se"}


def validate_public_image_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("image URL must be an absolute HTTPS URL")
    if host in BANNED_IMAGE_HOSTS or any(host.endswith(f".{item}") for item in BANNED_IMAGE_HOSTS):
        raise ValueError(f"image host {host} is prohibited")


def build_rich_content(blocks: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Build a showcase only when every block has a verified public URL."""

    if not blocks:
        raise ValueError("at least one rich-content block is required")
    showcase_blocks: list[dict[str, Any]] = []
    for block in blocks:
        url = block.get("url") or ""
        title = block.get("title") or ""
        text = block.get("text") or ""
        validate_public_image_url(url)
        if not title or not text:
            raise ValueError("every rich-content block requires title and text")
        showcase_blocks.append(
            {
                "imgLink": url,
                "img": {"src": url, "srcMobile": url},
                "title": {
                    "content": None,
                    "size": "size1",
                    "color": "color1",
                    "align": "left",
                },
                "text": {
                    "content": None,
                    "size": "size2",
                    "color": "color1",
                    "align": "left",
                },
            }
        )
    result = {
        "content": [
            {"widgetName": "raShowcase", "type": "billboard", "blocks": showcase_blocks}
        ],
        "version": 0.3,
    }
    validate_rich_content(result)
    return result


def validate_rich_content(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("rich content must be an object, not a bare array")
    if set(value) != {"content", "version"} or value.get("version") != 0.3:
        raise ValueError("rich content must have exactly content and version=0.3")
    content = value.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("rich content must contain at least one component")
    for component in content:
        if not isinstance(component, Mapping) or component.get("widgetName") != "raShowcase":
            raise ValueError("every top-level component must be raShowcase")
        blocks = component.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("raShowcase must contain blocks")
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ValueError("showcase block must be an object")
            required = {"imgLink", "img", "text", "title"}
            if not required.issubset(block):
                raise ValueError("each showcase block requires imgLink, img, text, and title")
            if not isinstance(block["text"], Mapping) or block["text"].get("content") is not None:
                raise ValueError("showcase block text.content must be null")
            if not isinstance(block["title"], Mapping) or block["title"].get("content") is not None:
                raise ValueError("showcase block title.content must be null")
            validate_public_image_url(str(block["imgLink"]))

