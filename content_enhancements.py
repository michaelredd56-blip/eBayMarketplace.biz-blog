from __future__ import annotations

from urllib.parse import urlparse

from flask import current_app

from models import Post, Product, db


def valid_image_url(value: str | None) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def refresh_product_image(product: Product | None) -> bool:
    """Populate a missing product image only from the verified source product page."""
    if not product or valid_image_url(product.image_url) or not product.source_url:
        return False

    # Import lazily so this enhancement can be installed before app.py imports services.
    from services import extract_product

    try:
        refreshed = extract_product(product.source_url)
    except Exception:
        current_app.logger.warning(
            "Could not refresh product image for product %s", product.id, exc_info=True
        )
        return False

    candidate = (refreshed or {}).get("image_url", "").strip()
    if not valid_image_url(candidate):
        return False

    product.image_url = candidate
    return True


def ensure_post_product_image(post: Post | None) -> bool:
    return bool(post and refresh_product_image(post.product))


def install_generation_image_enhancement(service_layer) -> None:
    """Ensure generated posts use a verified product image whenever one is available."""
    if getattr(service_layer.generate_post, "_product_image_enhanced", False):
        return

    original_generate_post = service_layer.generate_post

    def generate_post_with_product_image(*args, **kwargs):
        post = original_generate_post(*args, **kwargs)
        if ensure_post_product_image(post):
            db.session.commit()
        return post

    generate_post_with_product_image._product_image_enhanced = True
    service_layer.generate_post = generate_post_with_product_image
