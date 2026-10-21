"""Builders and validators for OZON Rich-Контент 0.3."""

from __future__ import annotations

from copy import deepcopy
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
                "imgLink": "",
                "img": {"src": url, "srcMobile": url},
                "title": {
                    "content": [title],
                    "size": "size1",
                    "color": "color1",
                    "align": "left",
                },
                "text": {
                    "content": [text],
                    "size": "size2",
                    "color": "color1",
                    "align": "left",
                },
            }
        )
    # Ozon's billboard template is one block per widget.  Multiple product
    # modules are represented by multiple raShowcase components.
    result = {
        "content": [
            {"widgetName": "raShowcase", "type": "billboard", "blocks": [block]}
            for block in showcase_blocks
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
        if component.get("type") != "billboard":
            raise ValueError("raShowcase.type must be billboard")
        blocks = component.get("blocks")
        if not isinstance(blocks, list) or len(blocks) != 1:
            raise ValueError("billboard raShowcase must contain exactly one block")
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ValueError("showcase block must be an object")
            required = {"imgLink", "img", "text", "title"}
            if not required.issubset(block):
                raise ValueError("each showcase block requires imgLink, img, text, and title")
            for field in ("title", "text"):
                text_block = block[field]
                if not isinstance(text_block, Mapping):
                    raise ValueError(f"showcase block {field} must be an object")
                text_content = text_block.get("content")
                if (
                    not isinstance(text_content, list)
                    or not text_content
                    or any(not isinstance(item, str) or not item.strip() for item in text_content)
                ):
                    raise ValueError(
                        f"showcase block {field}.content must be a non-empty string array"
                    )
                if text_block.get("size") not in {
                    "size1",
                    "size2",
                    "size3",
                    "size4",
                    "size5",
                }:
                    raise ValueError(f"showcase block {field}.size is invalid")
                if text_block.get("color") not in {"color1", "color2"}:
                    raise ValueError(f"showcase block {field}.color is invalid")
                if text_block.get("align") not in {"left", "center", "right"}:
                    raise ValueError(f"showcase block {field}.align is invalid")
            image = block["img"]
            if not isinstance(image, Mapping):
                raise ValueError("showcase block img must be an object")
            validate_public_image_url(str(image.get("src") or ""))
            validate_public_image_url(str(image.get("srcMobile") or ""))
            image_link = block.get("imgLink")
            if image_link:
                validate_public_image_url(str(image_link))


def upgrade_legacy_rich_content(
    value: Any,
    *,
    fallback_title: str = "Подробности товара",
    fallback_text: str = "Описание и преимущества товара",
) -> dict[str, Any]:
    """Upgrade the ERP's former ``null`` text blocks without another AI call.

    Only the exact legacy shape emitted by this project is repaired.  Every
    other structural error is left for :func:`validate_rich_content` to reject.
    This lets existing paid image results be reused safely after deployment.
    """

    if not isinstance(value, Mapping):
        raise ValueError("rich content must be an object, not a bare array")
    upgraded = deepcopy(dict(value))
    content = upgraded.get("content")
    if isinstance(content, list):
        for component in content:
            if not isinstance(component, dict):
                continue
            blocks = component.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                for field, fallback in (
                    ("title", fallback_title),
                    ("text", fallback_text),
                ):
                    text_block = block.get(field)
                    if (
                        isinstance(text_block, dict)
                        and text_block.get("content") is None
                    ):
                        text_block["content"] = [fallback]
        normalized_content: list[Any] = []
        for component in content:
            if (
                isinstance(component, dict)
                and component.get("widgetName") == "raShowcase"
                and component.get("type") == "billboard"
                and isinstance(component.get("blocks"), list)
                and len(component["blocks"]) > 1
            ):
                for block in component["blocks"]:
                    normalized = deepcopy(component)
                    normalized["blocks"] = [deepcopy(block)]
                    normalized_content.append(normalized)
            else:
                normalized_content.append(component)
        upgraded["content"] = normalized_content
    validate_rich_content(upgraded)
    return upgraded

