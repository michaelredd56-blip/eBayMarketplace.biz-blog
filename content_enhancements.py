from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup
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


def embed_product_image_in_article(post: Post | None) -> bool:
    """Insert the selected product's verified image directly into the article body.

    The generator uses this after it has selected either a random product or a
    manually requested product. Existing matching images are preserved without
    adding duplicates.
    """
    if not post or not post.product or not valid_image_url(post.product.image_url):
        return False

    image_url = post.product.image_url.strip()
    soup = BeautifulSoup(post.content_html or "", "html.parser")

    for image in soup.select("img[src]"):
        if image.get("src", "").strip() == image_url:
            return False

    figure = soup.new_tag("figure")
    image = soup.new_tag(
        "img",
        src=image_url,
        alt=post.product.title,
        loading="lazy",
    )
    figure.append(image)

    caption = soup.new_tag("figcaption")
    caption.string = f"Featured product: {post.product.title}"
    figure.append(caption)

    # Place the image near the top of the written article, immediately before
    # the first section heading. If no heading exists, make it the first item.
    first_heading = soup.find(["h2", "h3", "h4"])
    if first_heading:
        first_heading.insert_before(figure)
    else:
        soup.insert(0, figure)

    post.content_html = str(soup)
    return True


def install_generation_image_enhancement(service_layer) -> None:
    """Add the chosen product image to every generated post when available."""
    if getattr(service_layer.generate_post, "_product_image_enhanced", False):
        return

    original_generate_post = service_layer.generate_post

    def generate_post_with_product_image(*args, **kwargs):
        # The original generator chooses the product. This works identically for
        # random generation and for an explicitly supplied product_id.
        post = original_generate_post(*args, **kwargs)
        changed = ensure_post_product_image(post)
        changed = embed_product_image_in_article(post) or changed
        if changed:
            db.session.commit()
        return post

    generate_post_with_product_image._product_image_enhanced = True
    service_layer.generate_post = generate_post_with_product_image
