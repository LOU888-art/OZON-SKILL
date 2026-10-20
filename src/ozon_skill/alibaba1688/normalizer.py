"""Normalize ``alibaba.product.get`` responses without inventing data."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence


_MISSING = object()


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key, _MISSING)
        if value is not _MISSING and value is not None and value != "":
            return value
    return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _integer(value: Any) -> int | None:
    number = _number(value)
    if isinstance(number, int):
        return number
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _unwrap_product(response: Mapping[str, Any]) -> Mapping[str, Any]:
    current: Mapping[str, Any] = response
    for _ in range(4):
        for key in ("productInfo", "product", "result", "data"):
            nested = current.get(key)
            if isinstance(nested, Mapping):
                current = nested
                break
        else:
            return current
    return current


def _image_item(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"url": value, "width": None, "height": None}
    if not isinstance(value, Mapping):
        return None
    url = _first(value, "url", "imageUrl", "imageURL", "originalUrl", "originalURL")
    if url is None:
        return None
    return {
        "url": str(url),
        "width": _integer(_first(value, "width", "imageWidth", "originalWidth")),
        "height": _integer(_first(value, "height", "imageHeight", "originalHeight")),
    }


def _dedupe_images(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for value in values:
        item = _image_item(value)
        if item is None:
            continue
        url = item["url"]
        if url in positions:
            existing = result[positions[url]]
            if existing["width"] is None and item["width"] is not None:
                existing["width"] = item["width"]
            if existing["height"] is None and item["height"] is not None:
                existing["height"] = item["height"]
            continue
        positions[url] = len(result)
        result.append(item)
    return result


class _ImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = {key.lower(): value for key, value in attrs}
        url = values.get("src") or values.get("data-src") or values.get("data-lazyload")
        if not url:
            return
        self.images.append(
            {
                "url": url,
                "width": _integer(values.get("width")),
                "height": _integer(values.get("height")),
            }
        )


def _main_images(product: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    image_block = _first(product, "image", "productImage")
    if isinstance(image_block, Mapping):
        values = _first(image_block, "images", "imageList", default=[])
    else:
        values = _first(product, "images", "mainImages", "imageList", default=[])
    result = _dedupe_images(_sequence(values))
    return result or None


def _detail_images(product: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    explicit = _first(product, "detailImages", "descriptionImages", default=[])
    values = _sequence(explicit)
    description = _first(product, "description", "detail", "descriptionHtml")
    if isinstance(description, str):
        parser = _ImageParser()
        parser.feed(description)
        values.extend(parser.images)
    result = _dedupe_images(values)
    return result or None


def _attributes(product: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw = _first(
        product,
        "productAttributeList",
        "productAttribute",
        "attributes",
        "attributeList",
        default=[],
    )
    result: list[dict[str, Any]] = []
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            result.append({"name": str(name), "value": value})
    else:
        for item in _sequence(raw):
            if not isinstance(item, Mapping):
                continue
            name = _first(item, "attributeName", "name", "key")
            value = _first(item, "value", "attributeValue", "values")
            if name is not None:
                result.append({"name": str(name), "value": value})
    return result or None


def _specification(raw: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    parts: list[dict[str, Any]] = []
    for item in _sequence(raw):
        if isinstance(item, Mapping):
            name = _first(item, "attributeDisplayName", "attributeName", "name")
            value = _first(item, "attributeValue", "value", "valueName")
            if name is not None or value is not None:
                parts.append({"name": _text(name), "value": _text(value)})
        elif item is not None:
            parts.append({"name": None, "value": _text(item)})
    if not parts:
        return None, None
    text_parts = [
        f"{part['name']}:{part['value']}" if part["name"] else part["value"]
        for part in parts
        if part["value"]
    ]
    return parts, "; ".join(text_parts) or None


def _sku_list(product: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw = _first(
        product,
        "skuInfos",
        "skuInfoList",
        "productSkuInfos",
        "skus",
        default=[],
    )
    result: list[dict[str, Any]] = []
    for item in _sequence(raw):
        if not isinstance(item, Mapping):
            continue
        spec_raw = _first(
            item,
            "attributes",
            "skuAttributes",
            "specAttributes",
            "specification",
            default=[],
        )
        specification, specification_text = _specification(spec_raw)
        image_url = _first(item, "skuImageUrl", "imageUrl", "imageURL", "image")
        if image_url is None:
            for attribute in _sequence(spec_raw):
                if not isinstance(attribute, Mapping):
                    continue
                image_url = _first(
                    attribute, "skuImageUrl", "imageUrl", "imageURL", "image"
                )
                if image_url is not None:
                    break
        result.append(
            {
                "sku_id": _text(_first(item, "skuId", "skuID", "id", "specId")),
                "specification_zh": specification,
                "specification_text_zh": specification_text,
                "purchase_price_cny": _number(
                    _first(item, "price", "skuPrice", "offerPrice", "discountPrice")
                ),
                "stock": _integer(
                    _first(item, "amountOnSale", "stock", "quantity", "canBookCount")
                ),
                "image_url": _text(image_url),
            }
        )
    return result or None


def _category(product: Mapping[str, Any]) -> dict[str, Any]:
    category = _mapping(_first(product, "category", "categoryInfo", default={}))
    third = _first(product, "thirdCategoryId", "thirdCategoryID")
    if third is None:
        third = _first(category, "thirdCategoryId", "thirdCategoryID")
    category_id = _first(product, "categoryId", "categoryID")
    if category_id is None:
        category_id = _first(category, "categoryId", "categoryID", "id")
    selected = third if third is not None else category_id
    selected_from = "thirdCategoryId" if third is not None else (
        "categoryId" if category_id is not None else None
    )

    raw_path = _first(product, "categoryPath", "categoryPathList")
    if raw_path is None:
        raw_path = _first(category, "path", "categoryPath")
    path: list[dict[str, Any]] = []
    if isinstance(raw_path, str):
        names = [part.strip() for part in re.split(r"\s*[>/]\s*", raw_path) if part.strip()]
        path = [{"id": None, "name_zh": name} for name in names]
    else:
        for item in _sequence(raw_path):
            if isinstance(item, Mapping):
                path.append(
                    {
                        "id": _text(_first(item, "id", "categoryId", "categoryID")),
                        "name_zh": _text(_first(item, "name", "categoryName")),
                    }
                )
            elif item is not None:
                path.append({"id": None, "name_zh": _text(item)})

    return {
        "selected_category_id": _text(selected),
        "selected_from": selected_from,
        "path": path or None,
    }


def _supplier(product: Mapping[str, Any]) -> dict[str, Any]:
    supplier = _mapping(_first(product, "supplierInfo", "supplier", default={}))
    sale = _mapping(
        _first(product, "saleInfo", "productSaleInfo", "sale", default={})
    )
    shipping = _mapping(
        _first(product, "shippingInfo", "productShippingInfo", "shipping", default={})
    )

    company_name = _first(
        supplier, "companyName", "supplierName", "name", "loginId", "memberId"
    )
    if company_name is None:
        company_name = _first(
            product, "companyName", "supplierName", "supplierLoginId", "memberId"
        )
    minimum_order = _first(
        sale, "minOrderQuantity", "minimumOrderQuantity", "beginAmount"
    )
    if minimum_order is None:
        minimum_order = _first(
            product, "minOrderQuantity", "minimumOrderQuantity", "beginAmount"
        )
    ship_from = _first(
        shipping,
        "sendGoodsAddressText",
        "shipFrom",
        "location",
        "deliveryAddress",
    )
    if ship_from is None:
        province = _text(_first(shipping, "province", "provinceName"))
        city = _text(_first(shipping, "city", "cityName"))
        joined = " ".join(part for part in (province, city) if part)
        ship_from = joined or None

    return {
        "company_name": _text(company_name),
        "minimum_order_quantity": _number(minimum_order),
        "ship_from_zh": _text(ship_from),
    }


def normalize_product(
    response: Mapping[str, Any],
    requested_offer_id: str | int,
    *,
    collected_at: str | None = None,
    collection_mode: str = "live",
    source_api: str = "alibaba.product.get",
) -> dict[str, Any]:
    """Convert an official API response into the collector's stable schema."""

    product = _unwrap_product(response)
    offer_id = _first(product, "productID", "productId", "offerId", "offerID")
    if offer_id is None:
        offer_id = requested_offer_id
    timestamp = collected_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.0",
        "source_api": source_api,
        "collection_mode": collection_mode,
        "collected_at": timestamp,
        "source_offer_id": _text(offer_id),
        "title_zh": _text(_first(product, "subject", "title", "productName")),
        "images": _main_images(product),
        "detail_images": _detail_images(product),
        "sku_list": _sku_list(product),
        "attributes_zh": _attributes(product),
        "category_path_zh": _category(product),
        "supplier_info": _supplier(product),
    }

