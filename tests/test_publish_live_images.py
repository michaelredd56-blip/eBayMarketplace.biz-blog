from types import SimpleNamespace

from bs4 import BeautifulSoup

import services
from admin_enhancements import install_admin_enhancements
from app import create_app
from config import TestConfig
from content_enhancements import install_generation_image_enhancement
from models import Post, Product, db


def _product(slug: str, image_url: str = "https://images.example.com/product.jpg") -> Product:
    return Product(
        title=f"Product {slug}",
        slug=slug,
        source_url=f"https://ebaymarketplace.biz/{slug}",
        affiliate_url=f"https://www.ebay.com/itm/{slug}?campid=123",
        image_url=image_url,
        category="Electronics",
        description="A verified product used for publishing tests.",
        active=True,
    )


def _post(product: Product, slug: str, status: str = "draft") -> Post:
    return Post(
        product=product,
        title=f"Buying Guide {slug}",
        slug=slug,
        excerpt="A useful article ready for review and publication.",
        content_html="<p>Useful product information for shoppers.</p>",
        meta_title=f"Buying Guide {slug}"[:60],
        meta_description=(
            "Review this product, its important features, intended uses, value, and current listing before purchasing online."
        ),
        focus_keyword="buying guide",
        category="Shopping Guides",
        status=status,
    )


def _authenticate(client) -> None:
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "test-token"


def test_all_admin_article_surfaces_show_explicit_publish_actions():
    app = create_app(TestConfig)
    install_admin_enhancements(app)

    with app.app_context():
        draft_product = _product("draft")
        live_product = _product("live")
        db.session.add_all([draft_product, live_product])
        db.session.flush()
        draft = _post(draft_product, "draft", "draft")
        live = _post(live_product, "live", "published")
        db.session.add_all([draft, live])
        db.session.commit()
        draft_id = draft.id

    with app.test_client() as client:
        _authenticate(client)
        seo = client.get("/admin/seo")
        posts = client.get("/admin/posts")
        review = client.get(f"/admin/posts/{draft_id}/edit?next=/admin/seo")

    assert seo.status_code == 200
    assert b"Publish Live Now" in seo.data
    assert b"Republish Live" in seo.data
    assert posts.status_code == 200
    assert b"Publish Live Now" in posts.data
    assert b"Republish Live" in posts.data
    assert review.status_code == 200
    assert b"Save & Publish Live Now" in review.data
    assert b"Verified product image URL" in review.data


def test_review_save_and_publish_goes_live_with_manual_product_image():
    app = create_app(TestConfig)
    install_admin_enhancements(app)

    with app.app_context():
        product = _product("manual-image", image_url="")
        db.session.add(product)
        db.session.flush()
        post = _post(product, "manual-image", "draft")
        db.session.add(post)
        db.session.commit()
        post_id = post.id
        slug = post.slug

    image_url = "https://images.example.com/manual-product.jpg"
    with app.test_client() as client:
        _authenticate(client)
        response = client.post(
            f"/admin/posts/{post_id}/edit?next=/admin/seo",
            data={
                "csrf_token": "test-token",
                "next": "/admin/seo",
                "action": "publish",
                "title": "Reviewed Product Buying Guide",
                "excerpt": "A reviewed article ready to appear live on the website.",
                "meta_title": "Reviewed Product Buying Guide",
                "meta_description": (
                    "Read this reviewed product guide covering important features, intended uses, value, and the current listing."
                ),
                "focus_keyword": "reviewed product guide",
                "category": "Shopping Guides",
                "product_image_url": image_url,
                "content_html": "<p>This exact reviewed content should appear on the public website.</p>",
            },
        )
        live_page = client.get(f"/blog/{slug}")

    assert response.status_code == 302
    assert live_page.status_code == 200
    assert image_url.encode() in live_page.data
    assert b"This exact reviewed content should appear" in live_page.data

    with app.app_context():
        saved = db.session.get(Post, post_id)
        assert saved.status == "published"
        assert saved.product.image_url == image_url


def test_generated_article_recovers_and_embeds_verified_product_image(monkeypatch):
    app = create_app(TestConfig)

    with app.app_context():
        product = _product("generated", image_url="")
        db.session.add(product)
        db.session.flush()
        post = _post(product, "generated", "published")
        post.content_html = "<p>Opening product overview.</p><h2>Key features</h2><p>Details.</p>"
        db.session.add(post)
        db.session.commit()

        image_url = "https://images.example.com/generated-product.jpg"
        monkeypatch.setattr(
            services,
            "extract_product",
            lambda _url: {"image_url": image_url},
        )

        calls = []

        def original_generate(*args, **kwargs):
            calls.append((args, kwargs))
            return post

        fake_service_layer = SimpleNamespace(generate_post=original_generate)
        install_generation_image_enhancement(fake_service_layer)

        # No arguments represents random product generation.
        random_generated = fake_service_layer.generate_post()
        # A product_id represents the administrator explicitly choosing a product.
        selected_generated = fake_service_layer.generate_post(product.id, publish=True)

        assert random_generated is post
        assert selected_generated is post
        assert calls == [
            ((), {}),
            ((product.id,), {"publish": True}),
        ]
        assert post.product.image_url == image_url

        soup = BeautifulSoup(post.content_html, "html.parser")
        embedded_images = soup.select(f'img[src="{image_url}"]')
        assert len(embedded_images) == 1
        assert embedded_images[0].get("alt") == product.title
        assert embedded_images[0].get("loading") == "lazy"
        figure = embedded_images[0].find_parent("figure")
        assert figure is not None
        assert figure.find("figcaption").get_text(" ", strip=True) == f"Featured product: {product.title}"
        assert figure.find_next_sibling("h2") is not None
