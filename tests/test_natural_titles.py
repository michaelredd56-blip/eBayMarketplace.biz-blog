from types import SimpleNamespace

import services
from app import create_app
from config import TestConfig
from models import Post, Product, db
from title_enhancements import (
    build_meta_title,
    build_natural_seo_title,
    clean_product_name,
    install_natural_title_enhancement,
)


def _product(title: str) -> Product:
    return Product(
        title=title,
        slug="sony-headphones",
        source_url="https://ebaymarketplace.biz/sony-headphones",
        affiliate_url="https://www.ebay.com/itm/123?campid=456",
        image_url="https://images.example.com/sony.jpg",
        category="Electronics",
        description="Wireless noise-canceling headphones.",
        active=True,
    )


def _post(product: Product, slug: str = "robotic-title") -> Post:
    return Post(
        product=product,
        title=f"Is {product.title} Worth Considering? A Practical Buying Guide",
        slug=slug,
        excerpt="A useful product guide.",
        content_html=(
            f'<figure><img src="{product.image_url}" alt="{product.title}"></figure>'
            "<p>The reviewed article body must remain unchanged.</p>"
        ),
        meta_title="Is This Product Worth Considering?",
        meta_description=(
            "Review the product features, uses, buyer considerations, and listing details before making a purchase decision."
        ),
        focus_keyword="wireless headphones",
        category="Electronics",
        status="draft",
    )


def test_product_name_cleanup_removes_robotic_marketplace_clutter():
    cleaned = clean_product_name(
        "Sony WH-1000XM5 Wireless Headphones — Live Market Price and Current Listings | eBayMarketplace.biz"
    )

    assert cleaned == "Sony WH-1000XM5 Wireless Headphones"


def test_natural_titles_are_varied_product_led_and_not_question_formulas():
    product = _product("Sony WH-1000XM5 Wireless Headphones")
    titles = {
        build_natural_seo_title(product, post_id=index)
        for index in range(1, 20)
    }

    assert len(titles) >= 6
    for title in titles:
        lower = title.lower()
        assert "sony wh-1000xm5 wireless headphones" in lower
        assert not lower.startswith("is ")
        assert "worth considering" not in lower
        assert "live market price" not in lower
        assert "current listings" not in lower
        assert "?" not in title


def test_generation_wrapper_rewrites_title_slug_and_meta_without_changing_article():
    app = create_app(TestConfig)

    with app.app_context():
        product = _product(
            "Sony WH-1000XM5 Wireless Headphones — Live Market Price and Current Listings | eBayMarketplace.biz"
        )
        db.session.add(product)
        db.session.flush()
        post = _post(product)
        original_content = post.content_html
        db.session.add(post)
        db.session.commit()

        fake_service_layer = SimpleNamespace(
            generate_post=lambda *args, **kwargs: post,
            unique_slug=services.unique_slug,
        )
        install_natural_title_enhancement(fake_service_layer)
        generated = fake_service_layer.generate_post()

        assert generated is post
        assert generated.content_html == original_content
        assert generated.title.startswith("Is ") is False
        assert "Sony WH-1000XM5 Wireless Headphones" in generated.title
        assert "Live Market Price" not in generated.title
        assert "Current Listings" not in generated.title
        assert "Worth Considering" not in generated.title
        assert generated.slug != "robotic-title"
        assert len(generated.meta_title) <= 60
        assert generated.meta_title == build_meta_title(product, generated.title)


def test_repeated_product_articles_receive_distinct_natural_titles():
    app = create_app(TestConfig)

    with app.app_context():
        product = _product("Sony WH-1000XM5 Wireless Headphones")
        db.session.add(product)
        db.session.flush()
        first = _post(product, "first")
        second = _post(product, "second")
        db.session.add_all([first, second])
        db.session.commit()

        posts = iter([first, second])
        fake_service_layer = SimpleNamespace(
            generate_post=lambda *args, **kwargs: next(posts),
            unique_slug=services.unique_slug,
        )
        install_natural_title_enhancement(fake_service_layer)

        generated_first = fake_service_layer.generate_post()
        generated_second = fake_service_layer.generate_post()

        assert generated_first.title != generated_second.title
        assert generated_first.slug != generated_second.slug
