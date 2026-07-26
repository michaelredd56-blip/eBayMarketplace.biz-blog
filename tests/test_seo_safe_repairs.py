from bs4 import BeautifulSoup

import seo_repairs
from admin_enhancements import install_admin_enhancements
from app import create_app
from config import TestConfig
from models import Post, Product, SEOIssue, db, utcnow


def _product(slug: str = "example-product", image_url: str = "") -> Product:
    return Product(
        title=f"Example Product {slug}",
        slug=slug,
        source_url=f"https://ebaymarketplace.biz/{slug}",
        affiliate_url=f"https://www.ebay.com/itm/{slug}?campid=123",
        image_url=image_url,
        category="Electronics",
        description="A useful example product for testing the repair workflow.",
        active=True,
    )


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


def _draft_post(product: Product, slug: str) -> Post:
    return Post(
        product=product,
        title=f"Draft Buying Guide {slug}",
        slug=slug,
        excerpt="A draft article ready for review.",
        content_html="<p>Draft content.</p>",
        meta_title=f"Draft Buying Guide {slug}"[:60],
        meta_description=(
            "Review this draft product guide, its main features, intended uses, value, and current listing before publication."
        ),
        focus_keyword="draft product guide",
        category="Shopping Guides",
        status="draft",
    )


def test_safe_fixes_repair_and_reaudit_real_findings():
    app = create_app(TestConfig)

    with app.app_context():
        product = _product()
        db.session.add(product)
        db.session.flush()
        db.session.add_all([
            _published_post(product, "guide-one"),
            _published_post(product, "guide-two"),
        ])
        db.session.commit()

        audit = seo_repairs.run_seo_audit()
        assert audit["auto_fixable"] > 0
        assert audit["review_required"] == 4

        report = seo_repairs.apply_seo_fixes()
        assert report["attempted"] > 0
        assert report["applied"] > 0
        assert report["unresolved"] == 0

        posts = Post.query.order_by(Post.id).all()
        assert posts[0].meta_title != posts[1].meta_title
        assert all(70 <= len(post.meta_description) <= 160 for post in posts)
        assert product.image_url == ""

        for post in posts:
            soup = BeautifulSoup(post.content_html, "html.parser")
            affiliate = soup.select_one(f'a[href="{product.affiliate_url}"]')
            assert affiliate is not None
            assert affiliate.get_text(" ", strip=True) == product.title
            assert soup.select_one(f'a[href="{product.source_url}"]') is not None
            assert soup.select_one('a[href="https://ebaymarketplace.biz"]') is not None

        assert SEOIssue.query.filter_by(status="open", auto_fixable=True).count() == 0
        assert SEOIssue.query.filter_by(status="open", issue_type="thin-content").count() == 2
        assert SEOIssue.query.filter_by(status="open", issue_type="missing-image").count() == 2


def test_unconfirmed_image_is_never_counted_as_safe_fix():
    app = create_app(TestConfig)

    with app.app_context():
        product = _product("image-review")
        db.session.add(product)
        db.session.flush()
        post = _published_post(product, "image-review-guide")
        post.content_html = (
            "<p>" + "useful product information " * 600 + "</p>"
            f'<p><a href="{product.source_url}">eBayMarketplace.biz</a> '
            f'<a href="{product.affiliate_url}">{product.title}</a> '
            '<a href="https://ebaymarketplace.biz">Marketplace</a></p>'
        )
        post.meta_title = "Unique image review guide"
        post.meta_description = (
            "Review this product guide, its main features, intended uses, value, and current listing before buying online."
        )
        db.session.add(post)
        db.session.commit()

        audit = seo_repairs.run_seo_audit()
        assert audit["auto_fixable"] == 0
        assert audit["review_required"] == 1
        issue = SEOIssue.query.filter_by(issue_type="missing-image", status="open").one()
        assert issue.auto_fixable is False

        report = seo_repairs.apply_seo_fixes()
        assert report == {
            "attempted": 0,
            "applied": 0,
            "unresolved": 0,
            "unresolved_types": [],
        }


def test_seo_review_page_can_review_publish_and_delete_posts():
    app = create_app(TestConfig)
    install_admin_enhancements(app)

    with app.app_context():
        draft_product = _product("draft-product", "https://images.example.com/draft.jpg")
        live_product = _product("live-product", "https://images.example.com/live.jpg")
        db.session.add_all([draft_product, live_product])
        db.session.flush()
        draft = _draft_post(draft_product, "draft-guide")
        live = _published_post(live_product, "live-guide")
        db.session.add_all([draft, live])
        db.session.commit()
        draft_id = draft.id
        live_id = live.id
        seo_repairs.run_seo_audit()

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["admin_authenticated"] = True
            session["csrf_token"] = "test-token"

        page = client.get("/admin/seo")
        assert page.status_code == 200
        assert b"Review Content" in page.data
        assert b"Publish" in page.data
        assert b"Delete" in page.data
        assert b"Draft content review queue" in page.data

        publish = client.post(
            f"/admin/posts/{draft_id}/publish",
            data={"csrf_token": "test-token", "next": "/admin/seo"},
        )
        assert publish.status_code == 302

        delete = client.post(
            f"/admin/posts/{live_id}/delete",
            data={"csrf_token": "test-token", "next": "/admin/seo"},
        )
        assert delete.status_code == 302

    with app.app_context():
        assert db.session.get(Post, draft_id).status == "published"
        assert db.session.get(Post, live_id) is None


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
    assert b"Needs content review" in response.data
