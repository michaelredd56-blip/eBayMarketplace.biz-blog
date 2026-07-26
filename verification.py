from flask import Response

import services as service_layer
from seo_repairs import apply_seo_fixes, run_seo_audit

# Install the enhanced SEO functions before importing the Flask application.
# app.py imports these names from services while it registers the admin routes,
# and the automation cycle resolves run_seo_audit from this module at runtime.
service_layer.apply_seo_fixes = apply_seo_fixes
service_layer.run_seo_audit = run_seo_audit

from app import app  # noqa: E402
from admin_enhancements import install_admin_enhancements  # noqa: E402

install_admin_enhancements(app)


@app.get("/google8fa80b78c101f952.html")
def google_site_verification() -> Response:
    """Serve Google's URL-prefix property verification file at the site root."""
    response = Response(
        "google-site-verification: google8fa80b78c101f952.html\n",
        content_type="text/html; charset=utf-8",
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response
