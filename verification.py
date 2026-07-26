from flask import Response

import services as service_layer
from content_enhancements import install_generation_image_enhancement
from seo_repairs import apply_seo_fixes, run_seo_audit
from title_enhancements import install_natural_title_enhancement

# Install production enhancements before importing the Flask application so app.py
# binds its routes and automation cycle to the enhanced service functions.
service_layer.apply_seo_fixes = apply_seo_fixes
service_layer.run_seo_audit = run_seo_audit
install_generation_image_enhancement(service_layer)
install_natural_title_enhancement(service_layer)

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
