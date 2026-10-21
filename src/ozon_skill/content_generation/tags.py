"""Canonical Ozon hashtag formatting."""

from __future__ import annotations

import re
from typing import Any, Sequence


_HASHTAG_TOKEN = re.compile(r"#[^\s#]+")
_UNSAFE_CHARACTER = re.compile(r"[^0-9A-Za-zА-Яа-яЁё_]+")


def format_ozon_hashtags(values: Sequence[Any] | str) -> str:
    """Return the one string value expected by Ozon's ``# Теги`` field.

    Ozon does not accept an array for this attribute.  Every tag starts with
    ``#``, contains no spaces, and tags themselves are separated by one space.
    """

    raw_values: list[Any]
    if isinstance(values, str):
        existing = _HASHTAG_TOKEN.findall(values)
        raw_values = existing if existing else [values]
    else:
        raw_values = list(values)

    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        raw_text = str(raw or "").strip()
        parts = _HASHTAG_TOKEN.findall(raw_text) if "#" in raw_text else [raw_text]
        for part in parts:
            value = part.strip().lstrip("#").strip()
            value = re.sub(r"\s+", "_", value)
            value = _UNSAFE_CHARACTER.sub("_", value)
            value = re.sub(r"_+", "_", value).strip("_").lower()
            if not value:
                continue
            tag = f"#{value}"
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
            if len(result) == 30:
                return " ".join(result)
    return " ".join(result)
