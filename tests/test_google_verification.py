from verification import app


def test_google_site_verification_file():
    with app.test_client() as client:
        response = client.get("/google8fa80b78c101f952.html")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.get_data(as_text=True).strip() == (
        "google-site-verification: google8fa80b78c101f952.html"
    )
