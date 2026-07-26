from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup
from flask import current_app

from image_recovery import recover_product_image
from models import Post, Product, db


def valid_image_url(value: str | None) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def refresh_product_image(product: Product | None) -> bool:
    """Populate a missing product image from the curated page or live listing."""
    if not product or valid_image_url(product.image_url) or not product.source_url:
        return False

    # Use the existing structured product extractor first because it also follows
    # the source site's canonical product fields.
    from services import extract_product

    candidate = ""
    try:
        refreshed = extract_product(product.source_url)
        candidate = (refreshed or {}).get("image_url", "").strip()
    except Exception:
        current_app.logger.warning(
            "Structured product image refresh failed for product %s",
            product.id,
            exc_info=True,
        )

    # The source site may serialize images in Next.js data, lazy-load attributes,
    # or ordinary scripts instead of JSON-LD/og:image. The broader recovery path
    # checks those formats and then the live affiliate listing as a fallback.
    if not valid_image_url(candidate):
        candidate = recover_product_image(product.source_url, product.affiliate_url)

    if not valid_image_url(candidate):
        current_app.logger.warning(
            "No verified image could be recovered for product %s (%s)",
            product.id,
            product.title,
        )
        return False

    product.image_url = candidate.strip()
    return True


def ensure_post_product_image(post: Post | None) -> bool:
    return bool(post and refresh_product_image(post.product))


def article_contains_product_image(post: Post | None) -> bool:
    if not post or not post.product or not valid_image_url(post.product.image_url):
        return False
    image_url = post.product.image_url.strip()
    soup = BeautifulSoup(post.content_html or "", "html.parser")
    return any(
        image.get("src", "").strip() == image_url
        for image in soup.select("img[src]")
    )


def embed_product_image_in_article(post: Post | None) -> bool:
    """Insert the selected product's verified image directly into the article body."""
    if not post or not post.product or not valid_image_url(post.product.image_url):
        return False

    image_url = post.product.image_url.strip()
    soup = BeautifulSoup(post.content_html or "", "html.parser")

    for image in soup.select("img[src]"):
        if image.get("src", "").strip() == image_url:
            return False

    figure = soup.new_tag("figure")
    figure["class"] = "generated-product-image"
    image = soup.new_tag(
        "img",
        src=image_url,
        alt=post.product.title,
        loading="lazy",
    )
    image["width"] = "900"
    image["height"] = "675"
    figure.append(image)

    caption = soup.new_tag("figcaption")
    caption.string = f"Featured product: {post.product.title}"
    figure.append(caption)

    # Place the product photo near the top of the written article.
    first_heading = soup.find(["h2", "h3", "h4"])
    if first_heading:
        first_heading.insert_before(figure)
    else:
        soup.insert(0, figure)

    post.content_html = str(soup)
    return True


def install_generation_image_enhancement(service_layer) -> None:
    """Require every successfully generated article to contain its product image."""
    if getattr(service_layer.generate_post, "_product_image_enhanced", False):
        return

    original_select_product = service_layer.select_product_for_article
    original_generate_post = service_layer.generate_post

    def select_image_ready_product(product_id=None):
        # A manually selected product remains the requested product, but its image
        # is recovered before the article is written whenever possible.
        if product_id:
            product = original_select_product(product_id)
            if refresh_product_image(product):
                db.session.commit()
            return product

        # Random mode tries several distinct active products and prefers one with
        # a saved or recoverable image instead of silently choosing a blank record.
        active_count = Product.query.filter_by(active=True).count()
        attempts = min(max(active_count, 1), 12)
        seen_ids: set[int] = set()
        fallback = None

        for _ in range(attempts):
            product = original_select_product(None)
            fallback = product
            if product.id in seen_ids:
                continue
            seen_ids.add(product.id)
            if refresh_product_image(product):
                db.session.commit()
            if valid_image_url(product.image_url):
                return product

        return fallback or original_select_product(None)

    service_layer.select_product_for_article = select_image_ready_product

    def generate_post_with_product_image(*args, **kwargs):
        post = original_generate_post(*args, **kwargs)
        changed = ensure_post_product_image(post)
        changed = embed_product_image_in_article(post) or changed

        # Do not report success or publish an incomplete image-less article.
        if not article_contains_product_image(post):
            title = post.title
            db.session.delete(post)
            db.session.commit()
            raise ValueError(
                f'No usable product image could be retrieved for “{title}”. '
                "The incomplete article was not saved. Try generating again or choose another product."
            )

        if changed:
            db.session.commit()
        return post

    generate_post_with_product_image._product_image_enhanced = True
    service_layer.generate_post = generate_post_with_product_image
