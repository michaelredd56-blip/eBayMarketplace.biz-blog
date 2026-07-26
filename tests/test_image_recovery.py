import pytest

import content_enhancements
import image_recovery
import services
from app import create_app
from config import TestConfig
from content_enhancements import install_generation_image_enhancement
from models import Post, Product, db


def _product(slug: str, image_url: str = "") -> Product:
    return Product(
        title=f"Product {slug}",
        slug=slug,
        source_url=f"https://ebaymarketplace.biz/product/{slug}",
        affiliate_url=f"https://www.ebay.com/itm/{slug}?campid=123",
        image_url=image_url,
        category="Electronics",
        description="Product used to test image recovery.",
        active=True,
    )


def _post(product: Product, slug: str) -> Post:
    return Post(
        product=product,
        title=f"Buying Guide {slug}",
        slug=slug,
        excerpt="A test article.",
        content_html="<p>Opening.</p><h2>Details</h2><p>More information.</p>",
        meta_title=f"Buying Guide {slug}",
        meta_description="A detailed product guide with buying considerations and current listing information for shoppers.",
        focus_keyword="product guide",
        category="Shopping Guides",
        status="draft",
    )


def test_extracts_ebay_image_from_next_data_and_ignores_logo():
    html = """
    <html><head>
      <meta property="og:image" content="https://example.com/site-logo.png">
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"product":{"primaryImage":{"url":"https://i.ebayimg.com/images/g/abc/s-l1600.webp"}}}}}
      </script>
    </head></html>
    """
    assert image_recovery.extract_image_from_html(html, "https://ebaymarketplace.biz/product/1") == (
        "https://i.ebayimg.com/images/g/abc/s-l1600.webp"
    )


def test_extracts_lazy_loaded_product_image():
    html = """
    <html><body>
      <img src="/static/logo.svg" alt="Logo">
      <img data-src="https://i.ebayimg.com/images/g/xyz/s-l1200.jpg" alt="Product">
    </body></html>
    """
    assert image_recovery.extract_image_from_html(html, "https://ebaymarketplace.biz/product/2") == (
        "https://i.ebayimg.com/images/g/xyz/s-l1200.jpg"
    )


def test_image_less_generated_article_is_not_saved(monkeypatch):
    app = create_app(TestConfig)

    with app.app_context():
        product = _product("no-image")
        db.session.add(product)
        db.session.flush()
        post = _post(product, "no-image")
        db.session.add(post)
        db.session.commit()
        post_id = post.id

        monkeypatch.setattr(services, "extract_product", lambda _url: None)
        monkeypatch.setattr(content_enhancements, "recover_product_image", lambda *_args: "")

        class FakeServiceLayer:
            @staticmethod
            def select_product_for_article(product_id=None):
                return product

            @staticmethod
            def generate_post(*_args, **_kwargs):
                return post

        install_generation_image_enhancement(FakeServiceLayer)

        with pytest.raises(ValueError, match="incomplete article was not saved"):
            FakeServiceLayer.generate_post()

        assert db.session.get(Post, post_id) is None


def test_random_selection_prefers_recoverable_image_product(monkeypatch):
    app = create_app(TestConfig)

    with app.app_context():
        blank = _product("blank")
        ready = _product("ready", "https://i.ebayimg.com/images/g/ready/s-l1600.webp")
        db.session.add_all([blank, ready])
        db.session.flush()

        choices = iter([blank, ready])

        class FakeServiceLayer:
            @staticmethod
            def select_product_for_article(product_id=None):
                return ready if product_id else next(choices)

            @staticmethod
            def generate_post(*_args, **_kwargs):
                selected = FakeServiceLayer.select_product_for_article(None)
                post = _post(selected, "selected")
                db.session.add(post)
                db.session.commit()
                return post

        monkeypatch.setattr(services, "extract_product", lambda _url: None)
        monkeypatch.setattr(content_enhancements, "recover_product_image", lambda *_args: "")
        install_generation_image_enhancement(FakeServiceLayer)

        generated = FakeServiceLayer.generate_post()
        assert generated.product_id == ready.id
        assert ready.image_url in generated.content_html
