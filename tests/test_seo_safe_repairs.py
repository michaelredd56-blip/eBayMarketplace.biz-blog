from bs4 import BeautifulSoup

import seo_repairs
from app import create_app
from config import TestConfig
from models import Post, Product, SEOIssue, db, utcnow


def _published_post(product: Product, slug: str) -> Post:
    return Post(
        product=product,
        title=f"Useful Buying Guide {slug}",
        slug=slug,
        excerpt="A brief overview.",
        content_html="<p>Brief article.</p>",
        meta_title="Duplicate product guide",
        meta_description="Too short",
        focus_keyword="product guide",
        category="Shopping Guides",
        status="published",
        published_at=utcnow(),
    )


def test_safe_fixes_repair_and_reaudit_real_findings(monkeypatch):
    app = create_app(TestConfig)

    with app.app_context():
        product = Product(
            title="Example Product",
            slug="example-product",
            source_url="https://ebaymarketplace.biz/example-product",
            affiliate_url="https://www.ebay.com/itm/example?campid=123",
            image_url="",
            category="Electronics",
            description="A useful example product for testing the repair workflow.",
            active=True,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add_all([
            _published_post(product, "guide-one"),
            _published_post(product, "guide-two"),
        ])
        db.session.commit()

        monkeypatch.setattr(
            seo_repairs,
            "extract_product",
            lambda _url: {"image_url": "https://images.example.com/product.jpg"},
        )

        audit = seo_repairs.run_seo_audit()
        assert audit["auto_fixable"] > 0
        assert audit["review_required"] == 2

        fixed = seo_repairs.apply_seo_fixes()
        assert fixed > 0

        posts = Post.query.order_by(Post.id).all()
        assert posts[0].meta_title != posts[1].meta_title
        assert all(70 <= len(post.meta_description) <= 160 for post in posts)
        assert product.image_url == "https://images.example.com/product.jpg"

        for post in posts:
            soup = BeautifulSoup(post.content_html, "html.parser")
            affiliate = soup.select_one(f'a[href="{product.affiliate_url}"]')
            assert affiliate is not None
            assert affiliate.get_text(" ", strip=True) == product.title
            assert soup.select_one(f'a[href="{product.source_url}"]') is not None
            assert soup.select_one('a[href="https://ebaymarketplace.biz"]') is not None

        assert SEOIssue.query.filter_by(status="open", auto_fixable=True).count() == 0
        assert SEOIssue.query.filter_by(status="open", issue_type="thin-content").count() == 2


def test_seo_page_disables_button_when_only_review_items_exist():
    app = create_app(TestConfig)

    with app.app_context():
        db.session.add(
            SEOIssue(
                issue_key="post:1:thin-content",
                issue_type="thin-content",
                details="Article needs more original value.",
                auto_fixable=False,
                status="open",
            )
        )
        db.session.commit()

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["admin_authenticated"] = True
            session["csrf_token"] = "test-token"
        response = client.get("/admin/seo")

    assert response.status_code == 200
    assert b"No safe fixes available" in response.data
    assert b"Needs review" in response.data
