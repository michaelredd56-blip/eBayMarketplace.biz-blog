from config import TestConfig
from app import create_app
from models import Product, db
from services import generate_post, run_seo_audit


def make_app():
    return create_app(TestConfig)


def csrf(client, path="/admin/login"):
    client.get(path)
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def login(client):
    token = csrf(client)
    return client.post(
        "/admin/login",
        data={"csrf_token": token, "username": "admin", "password": "test-password"},
        follow_redirects=True,
    )


def test_public_home_and_health():
    app = make_app()
    with app.test_client() as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert b"Marketplace Finds" in client.get("/").data


def test_admin_login_and_manual_product():
    app = make_app()
    with app.test_client() as client:
        response = login(client)
        assert response.status_code == 200
        token = csrf(client, "/admin/products")
        response = client.post(
            "/admin/products",
            data={
                "csrf_token": token,
                "title": "Example Product",
                "source_url": "https://ebaymarketplace.biz/example-product",
                "affiliate_url": "https://www.ebay.com/itm/123?campid=1",
                "category": "Electronics",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            assert Product.query.count() == 1


def test_fallback_generation_publish_sitemap_and_audit():
    app = make_app()
    with app.app_context():
        product = Product(
            title="Example Product",
            slug="example-product",
            source_url="https://ebaymarketplace.biz/example-product",
            affiliate_url="https://www.ebay.com/itm/123?campid=1",
            category="Electronics",
            description="A sample product used to validate article generation.",
            active=True,
        )
        db.session.add(product)
        db.session.commit()
        post = generate_post(product.id, publish=True)
        assert post.status == "published"
        assert "sponsored" in post.content_html
        result = run_seo_audit()
        assert result["published_posts"] == 1
        post_slug = post.slug

    with app.test_client() as client:
        assert client.get(f"/blog/{post_slug}").status_code == 200
        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert post_slug.encode() in sitemap.data
