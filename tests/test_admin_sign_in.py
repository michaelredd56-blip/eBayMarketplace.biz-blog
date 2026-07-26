from app import create_app
from config import TestConfig


def test_admin_sign_in_page_is_visible_and_ready():
    app = create_app(TestConfig)

    with app.test_client() as client:
        home = client.get("/")
        login = client.get("/admin/login")

    assert home.status_code == 200
    assert b"Admin Sign In" in home.data
    assert login.status_code == 200
    assert b"Manage the product blog" in login.data
    assert b'value="admin"' in login.data
    assert b"ADMIN_PASSWORD" in login.data
    assert b"data-password-toggle" in login.data
