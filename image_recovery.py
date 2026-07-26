from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import current_app


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_IMAGE_KEYS = {
    "image",
    "images",
    "imageurl",
    "image_url",
    "thumbnail",
    "thumbnailurl",
    "thumbnail_url",
    "primaryimage",
    "primary_image",
    "mainimage",
    "main_image",
    "contenturl",
    "content_url",
    "picture",
    "pictures",
    "photo",
    "photos",
}

_REJECT_HINTS = (
    "logo",
    "favicon",
    "sprite",
    "avatar",
    "icon",
    "badge",
    "placeholder",
    "tracking",
    "pixel",
    "transparent",
)


def _normalize_url(value: object, base_url: str) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().replace("\\/", "/").replace("&amp;", "&")
    if not candidate or candidate.startswith(("data:", "blob:")):
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    candidate = urljoin(base_url, candidate)
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    lower = candidate.lower()
    if any(hint in lower for hint in _REJECT_HINTS):
        return ""
    return candidate


def _srcset_values(value: str) -> Iterable[str]:
    for item in (value or "").split(","):
        candidate = item.strip().split(" ", 1)[0].strip()
        if candidate:
            yield candidate


def _walk_json(value: object, base_url: str, parent_key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            image_key = (
                normalized_key in _IMAGE_KEYS
                or "image" in normalized_key
                or "thumbnail" in normalized_key
                or "picture" in normalized_key
            )
            if image_key and isinstance(child, str):
                candidate = _normalize_url(child, base_url)
                if candidate:
                    yield candidate
            elif image_key and isinstance(child, dict):
                for nested_key in ("url", "contentUrl", "content_url", "src", "href"):
                    candidate = _normalize_url(child.get(nested_key), base_url)
                    if candidate:
                        yield candidate
                yield from _walk_json(child, base_url, normalized_key)
            elif image_key and isinstance(child, list):
                for item in child:
                    if isinstance(item, str):
                        candidate = _normalize_url(item, base_url)
                        if candidate:
                            yield candidate
                    else:
                        yield from _walk_json(item, base_url, normalized_key)
            else:
                yield from _walk_json(child, base_url, normalized_key)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child, base_url, parent_key)
    elif isinstance(value, str) and any(token in parent_key for token in ("image", "thumbnail", "picture")):
        candidate = _normalize_url(value, base_url)
        if candidate:
            yield candidate


def _score(url: str, position: int) -> tuple[int, int]:
    lower = url.lower()
    host = urlparse(url).netloc.lower()
    score = 0
    if "ebayimg.com" in host:
        score += 120
    if any(token in lower for token in ("s-l1600", "s-l1200", "s-l960", "large", "original", "primary")):
        score += 35
    if re.search(r"\.(?:jpe?g|png|webp|avif)(?:[?&]|$)", lower):
        score += 20
    if any(token in lower for token in ("s-l64", "s-l96", "s-l140", "thumb", "thumbnail", "small")):
        score -= 25
    if lower.split("?", 1)[0].endswith(".svg"):
        score -= 100
    return score, -position


def extract_image_from_html(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[str] = []

    def add(value: object) -> None:
        candidate = _normalize_url(value, base_url)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for selector, attribute in (
        ('meta[property="og:image:secure_url"]', "content"),
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[name="twitter:image:src"]', "content"),
        ('meta[itemprop="image"]', "content"),
        ('link[rel="image_src"]', "href"),
    ):
        for node in soup.select(selector):
            add(node.get(attribute))

    for image in soup.select("img"):
        for attribute in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-image",
            "data-zoom-src",
        ):
            add(image.get(attribute))
        for attribute in ("srcset", "data-srcset"):
            for item in _srcset_values(image.get(attribute, "")):
                add(item)

    for source in soup.select("picture source, source[srcset]"):
        for item in _srcset_values(source.get("srcset", "")):
            add(item)

    for script in soup.select(
        'script[type="application/ld+json"], '
        'script[type="application/json"], '
        'script#__NEXT_DATA__'
    ):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for candidate in _walk_json(payload, base_url):
            add(candidate)

    # Dynamic product pages sometimes serialize CDN image URLs in ordinary scripts.
    for raw_url in re.findall(r"https?:\\?/\\?/[^\"'<>\s]+", html or "", flags=re.I):
        cleaned = raw_url.replace("\\/", "/")
        lower = cleaned.lower()
        if "ebayimg.com" in lower or re.search(r"\.(?:jpe?g|png|webp|avif)(?:[?&]|$)", lower):
            add(cleaned)

    if not candidates:
        return ""
    return max(enumerate(candidates), key=lambda item: _score(item[1], item[0]))[1]


def fetch_image_url(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(
            url,
            headers=_BROWSER_HEADERS,
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        current_app.logger.warning("Could not inspect product image source %s", url, exc_info=True)
        return ""
    return extract_image_from_html(response.text, response.url or url)


def recover_product_image(source_url: str, affiliate_url: str = "") -> str:
    """Recover the selected product image from the curated page or live listing."""
    for url in (source_url, affiliate_url):
        candidate = fetch_image_url(url)
        if candidate:
            return candidate
    return ""
