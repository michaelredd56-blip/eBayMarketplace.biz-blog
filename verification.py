from flask import Response

from app import app


@app.get("/google8fa80b78c101f952.html")
def google_site_verification() -> Response:
    """Serve Google's URL-prefix property verification file at the site root."""
    response = Response(
        "google-site-verification: google8fa80b78c101f952.html\n",
        content_type="text/html; charset=utf-8",
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response
