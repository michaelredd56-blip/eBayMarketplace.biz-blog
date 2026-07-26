from __future__ import annotations

import hashlib
import re
from html import unescape

from models import Post, Product, db


TITLE_TEMPLATES = (
    "{product}: Features, Uses and Buying Tips",
    "{product} Buying Guide: What to Know Before You Buy",
    "A Closer Look at {product}: Features and Buyer Considerations",
    "{product}: A Practical Guide for Smarter Shopping",
    "How to Choose {product}: Features and Shopping Tips",
    "{product} Explained: Features, Uses and Buying Advice",
    "{product}: What Buyers Should Compare Before Ordering",
    "Shopping for {product}: A Clear Buyer’s Guide",
    "{product}: Key Details, Practical Uses and Buying Tips",
    "{product} Guide: Features, Fit and Buyer Considerations",
    "Understanding {product}: Uses, Features and Shopping Advice",
    "{product}: A Straightforward Guide for Buyers",
)

ROBOTIC_TITLE_PATTERNS = (
    r"\blive market prices?\b",
    r"\bcurrent market prices?\b",
    r"\bcurrent listings?\b",
    r"\blatest listings?\b",
    r"\bprice(?:s)? and listings?\b",
    r"\bworth considering\b",
    r"\bworth buying\b",
    r"\bworth it\b",
)

SITE_SUFFIX_PATTERN = re.compile(
    r"\s*(?:\||—|–|-)\s*(?:ebaymarketplace\.biz|ebay marketplace(?:\.biz)?|ebay)\s*$",
    re.IGNORECASE,
)


def _collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _fit_words(value: str, limit: int) -> str:
    value = _collapse_space(value)
    if len(value) <= limit:
        return value
    shortened = value[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,:;|–—-")
    return shortened or value[:limit].rstrip(" ,:;|–—-")


def clean_product_name(value: str) -> str:
    """Keep useful product keywords while removing marketplace and robotic title clutter."""
    cleaned = _collapse_space(value)
    cleaned = SITE_SUFFIX_PATTERN.sub("", cleaned)
    for pattern in ROBOTIC_TITLE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[-–—|:]\s*(?:shop now|buy now|for sale)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*(?:[-–—|:,]\s*)?(?:and|or)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+([,.:;!?])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*[-–—|:,]\s*){2,}", " — ", cleaned)
    cleaned = cleaned.strip(" ,:;|–—-")
    return _fit_words(cleaned, 105) or "Featured Product"


def _template_start(product: Product, post_id: int | None) -> int:
    source = f"{product.title}|{product.category or ''}|{post_id or 0}".encode("utf-8")
    return int(hashlib.sha256(source).hexdigest()[:8], 16) % len(TITLE_TEMPLATES)


def build_natural_seo_title(
    product: Product,
    post_id: int | None = None,
    existing_titles: set[str] | None = None,
) -> str:
    """Create a varied editorial title with the product keyword naturally included."""
    product_name = clean_product_name(product.title)
    existing = {_collapse_space(title).casefold() for title in (existing_titles or set())}
    start = _template_start(product, post_id)

    for offset in range(len(TITLE_TEMPLATES)):
        template = TITLE_TEMPLATES[(start + offset) % len(TITLE_TEMPLATES)]
        candidate = _fit_words(template.format(product=product_name), 150)
        if candidate.casefold() not in existing:
            return candidate

    return _fit_words(f"{product_name}: Buyer’s Guide and Product Overview", 150)


def build_meta_title(product: Product, article_title: str) -> str:
    """Create a readable search title without cutting it mid-phrase when possible."""
    if len(article_title) <= 60:
        return article_title

    product_name = clean_product_name(product.title)
    candidates = (
        f"{product_name} Buying Guide",
        f"{product_name}: Features & Buying Tips",
        f"{product_name} Product Guide",
        product_name,
    )
    for candidate in candidates:
        if len(candidate) <= 60:
            return candidate
    return _fit_words(product_name, 60)


def install_natural_title_enhancement(service_layer) -> None:
    """Rewrite generated article titles before the post is returned to any admin or automation route."""
    if getattr(service_layer.generate_post, "_natural_title_enhanced", False):
        return

    original_generate_post = service_layer.generate_post

    def generate_post_with_natural_title(*args, **kwargs):
        post = original_generate_post(*args, **kwargs)
        if not post or not post.product:
            return post

        existing_titles = {
            row[0]
            for row in db.session.query(Post.title)
            .filter(Post.id != post.id)
            .all()
            if row[0]
        }
        title = build_natural_seo_title(post.product, post.id, existing_titles)
        post.title = title
        post.meta_title = build_meta_title(post.product, title)
        post.slug = service_layer.unique_slug(Post, title, post.id)
        db.session.commit()
        return post

    generate_post_with_natural_title._natural_title_enhanced = True
    service_layer.generate_post = generate_post_with_natural_title
