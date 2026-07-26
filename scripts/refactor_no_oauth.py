from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Could not find expected {label}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"Expected one {label} match, found {count}")
    return updated


# app.py: remove Search Console/OAuth routes and make sitemap URLs host-correct.
app = read("app.py")
app = app.replace("import hmac\n", "import hmac\nimport threading\n", 1)
app = app.replace("\nimport gsc\n", "\n")
app = app.replace(
    "from models import AutomationRun, OAuthToken, Post, Product, SEOIssue, db, utcnow",
    "from models import AutomationRun, Post, Product, SEOIssue, db, utcnow",
)
for name in (
    "    delete_oauth_token,\n",
    "    save_oauth_token,\n",
    "    sync_search_console_signals,\n",
    "    record_url_inspection,\n",
    "    setting_set,\n",
):
    app = app.replace(name, "")
app = replace_once(
    app,
    "        canonical = f\"{app.config['SITE_URL']}/blog/{post.slug}\"\n        return render_template(\"article.html\", post=post, related=related, canonical=canonical)",
    """        canonical = url_for(\"article\", slug=post.slug, _external=True)
        blog_post = {
            \"@type\": \"BlogPosting\",
            \"headline\": post.title,
            \"description\": post.meta_description,
            \"url\": canonical,
            \"mainEntityOfPage\": {\"@type\": \"WebPage\", \"@id\": canonical},
            \"datePublished\": post.published_at.isoformat() if post.published_at else \"\",
            \"dateModified\": post.updated_at.isoformat(),
            \"publisher\": {
                \"@type\": \"Organization\",
                \"name\": app.config[\"SITE_NAME\"],
                \"url\": request.url_root.rstrip(\"/\"),
            },
        }
        graph = [blog_post]
        if post.product:
            product_id = canonical + \"#product\"
            product_schema = {
                \"@type\": \"Product\",
                \"@id\": product_id,
                \"name\": post.product.title,
                \"description\": post.product.description or post.excerpt,
                \"url\": post.product.affiliate_url,
                \"offers\": {\"@type\": \"Offer\", \"url\": post.product.affiliate_url},
            }
            if post.product.image_url:
                product_schema[\"image\"] = [post.product.image_url]
                blog_post[\"image\"] = [post.product.image_url]
            if post.product.brand:
                product_schema[\"brand\"] = {\"@type\": \"Brand\", \"name\": post.product.brand}
            if post.product.price:
                product_schema[\"offers\"][\"price\"] = post.product.price
                product_schema[\"offers\"][\"priceCurrency\"] = post.product.currency or \"USD\"
            blog_post[\"mainEntity\"] = {\"@id\": product_id}
            graph.append(product_schema)
        article_schema = {\"@context\": \"https://schema.org\", \"@graph\": graph}
        return render_template(
            \"article.html\", post=post, related=related, canonical=canonical, article_schema=article_schema
        )""",
    "article schema route",
)
app = replace_once(
    app,
    "        body = f\"User-agent: *\\nAllow: /\\nDisallow: /admin/\\nSitemap: {app.config['SITE_URL']}/sitemap.xml\\n\"",
    "        body = f\"User-agent: *\\nAllow: /\\nDisallow: /admin/\\nSitemap: {url_for('sitemap', _external=True)}\\n\"",
    "robots sitemap URL",
)
app = regex_once(
    app,
    r'    @app\.get\("/sitemap\.xml"\)\n    def sitemap\(\):.*?\n\n    @app\.get\("/feed\.xml"\)',
    '''    @app.get("/sitemap.xml")
    def sitemap():
        base_url = request.url_root.rstrip("/")
        posts = Post.query.filter_by(status="published").order_by(desc(Post.updated_at)).all()
        home_modified = posts[0].updated_at if posts else utcnow()
        static_urls = [
            (base_url + "/", home_modified, "daily", "1.0"),
            (base_url + "/about", home_modified, "monthly", "0.5"),
            (base_url + "/privacy", home_modified, "yearly", "0.3"),
            (base_url + "/affiliate-disclosure", home_modified, "yearly", "0.3"),
        ]
        response = Response(
            render_template("sitemap.xml", static_urls=static_urls, posts=posts, base_url=base_url),
            content_type="application/xml; charset=utf-8",
        )
        response.headers["Cache-Control"] = "public, max-age=300"
        response.set_etag(f"{int(home_modified.timestamp())}-{len(posts)}")
        response.last_modified = home_modified
        return response

    @app.get("/feed.xml")''',
    "sitemap route",
)
app = replace_once(
    app,
    '''        result = run_seo_audit()
        gsc_result = sync_search_console_signals()
        flash(f"Audit completed. {result['open_issues']} open issues; {result['auto_fixable']} can be repaired automatically. Search Console sync: {gsc_result['status']}.", "success")''',
    '''        result = run_seo_audit()
        flash(
            f"Audit completed. {result['open_issues']} open issues; "
            f"{result['auto_fixable']} can be repaired automatically.",
            "success",
        )''',
    "SEO audit message",
)
app = regex_once(
    app,
    r'\n    @app\.get\("/admin/gsc"\).*?\n\n\ndef start_scheduler',
    "\n\n\ndef start_scheduler",
    "Search Console routes",
)
app = replace_once(
    app,
    '''    scheduler.start()
    app.extensions["content_scheduler"] = scheduler''',
    '''    scheduler.start()
    app.extensions["content_scheduler"] = scheduler

    # Render free services can restart or sleep through the scheduled hour. Run one
    # duplicate-protected cycle at startup so the blog still publishes once per day.
    threading.Thread(
        target=scheduled_cycle,
        name="startup-content-cycle",
        daemon=True,
    ).start()''',
    "startup automation",
)
write("app.py", app)


# services.py: remove OAuth storage and force exact product-name affiliate anchors.
services = read("services.py")
services = services.replace("import base64\n", "")
services = services.replace("from cryptography.fernet import Fernet\n", "")
services = services.replace(
    "from models import AutomationRun, OAuthToken, Post, Product, SEOIssue, SiteSetting, db, utcnow",
    "from models import AutomationRun, Post, Product, SEOIssue, db, utcnow",
)
services = regex_once(
    services,
    r'\ndef setting_get\(key: str, default: str = ""\) -> str:.*?\n\ndef _parse_xml_locations',
    "\n\ndef _parse_xml_locations",
    "OAuth token helpers",
)
services = services.replace(
    'or <a href="{escape(product.affiliate_url)}" target="_blank" rel="sponsored noopener nofollow">view the current eBay listing</a>.',
    'or view <a href="{escape(product.affiliate_url)}" target="_blank" rel="sponsored noopener nofollow">{escape(product.title)}</a>.',
)
services = services.replace(
    '- Include one clear call-to-action link to the affiliate product URL with rel="sponsored noopener nofollow" and target="_blank".\n- Include one natural contextual link to the marketplace homepage using varied anchor text, not an exact-match stuffed keyword.',
    '- Link the exact product title as the visible anchor text to the affiliate product URL. Use rel="sponsored noopener nofollow" and target="_blank".\n- Include one natural contextual link to the marketplace homepage.',
)
services = regex_once(
    services,
    r'def ensure_required_links\(content_html: str, product: Product\) -> str:.*?\n\n\ndef generate_post',
    '''def ensure_required_links(content_html: str, product: Product) -> str:
    """Guarantee visible, compliant product-name affiliate linking on every article."""
    soup = BeautifulSoup(content_html, "html.parser")
    affiliate_anchor = None
    for anchor in soup.select("a[href]"):
        if anchor.get("href", "") == product.affiliate_url:
            affiliate_anchor = anchor
            break

    if affiliate_anchor is None:
        paragraph = soup.new_tag("p")
        paragraph.append("View the current listing for ")
        affiliate_anchor = soup.new_tag("a", href=product.affiliate_url)
        paragraph.append(affiliate_anchor)
        paragraph.append(".")
        soup.append(paragraph)

    affiliate_anchor.clear()
    affiliate_anchor.append(product.title)
    affiliate_anchor["target"] = "_blank"
    affiliate_anchor["rel"] = "sponsored noopener nofollow"

    hrefs = [a.get("href", "") for a in soup.select("a[href]")]
    if product.source_url not in hrefs:
        paragraph = soup.new_tag("p")
        paragraph.append("See the curated product page on ")
        link = soup.new_tag("a", href=product.source_url)
        link.string = "eBayMarketplace.biz"
        link["rel"] = "noopener"
        paragraph.append(link)
        paragraph.append(".")
        soup.append(paragraph)

    source_home = current_app.config["SOURCE_SITE_URL"]
    if not any(h.rstrip("/") == source_home.rstrip("/") for h in hrefs):
        paragraph = soup.new_tag("p")
        paragraph.append("Browse more product selections at ")
        link = soup.new_tag("a", href=source_home)
        link.string = "eBayMarketplace.biz"
        link["rel"] = "noopener"
        paragraph.append(link)
        paragraph.append(".")
        soup.append(paragraph)
    return str(soup)


def generate_post''',
    "affiliate link enforcement",
)
services = replace_once(
    services,
    '''        if post.product and post.product.affiliate_url not in hrefs:
            checks.append(("missing-affiliate-link", "The article is missing the product affiliate link.", True))''',
    '''        if post.product and post.product.affiliate_url not in hrefs:
            checks.append(("missing-affiliate-link", "The article is missing the product affiliate link.", True))
        if post.product:
            matching = [
                anchor for anchor in soup.select("a[href]")
                if anchor.get("href", "") == post.product.affiliate_url
            ]
            if not matching or matching[0].get_text(" ", strip=True) != post.product.title:
                checks.append((
                    "affiliate-anchor-text",
                    "The affiliate link must use the exact product name as its visible anchor text.",
                    True,
                ))''',
    "affiliate anchor audit",
)
services = services.replace(
    'elif issue.issue_type in {"missing-source-link", "missing-affiliate-link", "missing-marketplace-link"} and post.product:',
    'elif issue.issue_type in {"missing-source-link", "missing-affiliate-link", "affiliate-anchor-text", "missing-marketplace-link"} and post.product:',
)
services = services.replace('        details["search_console"] = sync_search_console_signals()\n', "")
services = re.sub(r'\n\ndef sync_search_console_signals\(\) -> dict:.*\Z', "\n", services, flags=re.S)
write("services.py", services)


# Remove OAuth configuration and use Render's automatic public URL.
config = read("config.py")
config = config.replace(
    '    SITE_URL = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")',
    '    SITE_URL = (os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:5000").rstrip("/")',
)
config = regex_once(
    config,
    r'\n    GOOGLE_CLIENT_ID = os\.getenv\("GOOGLE_CLIENT_ID", ""\).*?TOKEN_ENCRYPTION_KEY = os\.getenv\("TOKEN_ENCRYPTION_KEY", ""\)\n',
    "\n",
    "Google OAuth configuration",
)
write("config.py", config)


# Remove OAuth-only database models.
models = read("models.py")
models = regex_once(
    models,
    r'\n\nclass OAuthToken\(db\.Model\):.*?\n\nclass SEOIssue',
    "\n\nclass SEOIssue",
    "OAuth database models",
)
write("models.py", models)


# Remove OAuth libraries.
requirements = read("requirements.txt")
requirements = "\n".join(
    line for line in requirements.splitlines()
    if not line.startswith(("google-api-python-client", "google-auth", "google-auth-oauthlib", "cryptography"))
) + "\n"
write("requirements.txt", requirements)


# Remove Search Console from the admin navigation.
admin_base = read("templates/admin_base.html")
admin_base = admin_base.replace('<a href="{{ url_for(\'admin_gsc\') }}">Search Console</a>', "")
write("templates/admin_base.html", admin_base)


# Stronger BlogPosting + Product schema and exact product-name CTA.
write("templates/article.html", '''{% extends "base.html" %}
{% block title %}{{ post.meta_title }}{% endblock %}
{% block meta_description %}{{ post.meta_description }}{% endblock %}
{% block canonical %}{{ canonical }}{% endblock %}
{% block head %}
<meta property="og:type" content="article">
<meta property="og:title" content="{{ post.meta_title }}">
<meta property="og:description" content="{{ post.meta_description }}">
<meta property="og:url" content="{{ canonical }}">
{% if post.product and post.product.image_url %}<meta property="og:image" content="{{ post.product.image_url }}">{% endif %}
<script type="application/ld+json">{{ article_schema|tojson }}</script>
{% endblock %}
{% block content %}
<article class="article-shell">
  <header class="article-hero">
    <div class="container narrow">
      <a class="card-category" href="{{ url_for('category', category=post.category|lower|replace(' ', '-')) }}">{{ post.category }}</a>
      <h1>{{ post.title }}</h1>
      <p class="article-deck">{{ post.excerpt }}</p>
      <div class="article-meta">Published {{ post.published_at|datefmt }} · Updated {{ post.updated_at|datefmt }}</div>
    </div>
  </header>
  {% if post.product and post.product.image_url %}
  <div class="container article-image-wrap"><img class="article-image" src="{{ post.product.image_url }}" alt="{{ post.product.title }}"></div>
  {% endif %}
  <div class="container article-layout">
    <div class="article-content">{{ post.content_html|safe }}</div>
    {% if post.product %}
    <aside class="product-cta">
      <span>Featured product</span>
      {% if post.product.image_url %}<img src="{{ post.product.image_url }}" alt="{{ post.product.title }}" loading="lazy">{% endif %}
      <h2>{{ post.product.title }}</h2>
      {% if post.product.price %}<strong>{{ post.product.price|money(post.product.currency) }}</strong>{% endif %}
      <a class="button" href="{{ post.product.affiliate_url }}" target="_blank" rel="sponsored noopener nofollow">{{ post.product.title }}</a>
      <a class="text-link" href="{{ post.product.source_url }}" rel="noopener">See curated marketplace page</a>
      <small>Price, condition, and availability are controlled by the seller and may change.</small>
    </aside>
    {% endif %}
  </div>
</article>
{% if related %}
<section class="container section-block"><div class="section-heading"><h2>Related guides</h2></div><div class="card-grid compact">{% for item in related %}<article class="post-card"><div class="card-body"><span class="card-category">{{ item.category }}</span><h3><a href="{{ url_for('article', slug=item.slug) }}">{{ item.title }}</a></h3><p>{{ item.excerpt }}</p></div></article>{% endfor %}</div></section>
{% endif %}
{% endblock %}
''')


# Dynamic root sitemap: every published post appears immediately.
write("templates/sitemap.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{% for location, modified, frequency, priority in static_urls %}
  <url><loc>{{ location }}</loc><lastmod>{{ modified.date().isoformat() }}</lastmod><changefreq>{{ frequency }}</changefreq><priority>{{ priority }}</priority></url>
{% endfor %}
{% for post in posts %}
  <url><loc>{{ base_url }}/blog/{{ post.slug }}</loc><lastmod>{{ post.updated_at.date().isoformat() }}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>
{% endfor %}
</urlset>
''')


# Render Blueprint: no Google/OAuth fields, automatic daily publishing enabled.
write("render.yaml", '''services:
  - type: web
    name: ebay-marketplace-blog
    runtime: python
    plan: free
    autoDeploy: true
    buildCommand: pip install --upgrade pip && pip install -r requirements.txt
    startCommand: gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 app:app
    healthCheckPath: /healthz
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.8
      - key: DATABASE_URL
        fromDatabase:
          name: ebay-marketplace-blog-db
          property: connectionString
      - key: SECRET_KEY
        generateValue: true
      - key: ADMIN_USERNAME
        value: admin
      - key: ADMIN_PASSWORD
        sync: false
      - key: SITE_NAME
        value: Marketplace Finds
      - key: SITE_TAGLINE
        value: Useful product guides and smart shopping ideas
      - key: SOURCE_SITE_URL
        value: https://ebaymarketplace.biz
      - key: SOURCE_SITEMAP_URL
        value: https://ebaymarketplace.biz/sitemap.xml
      - key: OPENAI_API_KEY
        sync: false
      - key: OPENAI_MODEL
        value: gpt-4o-mini
      - key: AUTO_PUBLISH
        value: "true"
      - key: SCHEDULER_ENABLED
        value: "true"
      - key: POSTS_PER_RUN
        value: "1"
      - key: AUTOMATION_HOUR_UTC
        value: "13"
      - key: PRODUCT_IMPORT_LIMIT
        value: "25"
      - key: SESSION_COOKIE_SECURE
        value: "true"

databases:
  - name: ebay-marketplace-blog-db
    plan: free
    databaseName: marketplace_blog
    user: marketplace_blog
''')


write(".env.example", '''SECRET_KEY=replace-with-a-long-random-secret
DATABASE_URL=sqlite:///blog.db
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-password
SITE_NAME=Marketplace Finds
SITE_TAGLINE=Useful product guides and smart shopping ideas
SITE_URL=http://localhost:5000
SOURCE_SITE_URL=https://ebaymarketplace.biz
SOURCE_SITEMAP_URL=https://ebaymarketplace.biz/sitemap.xml
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
AUTO_PUBLISH=true
SCHEDULER_ENABLED=true
POSTS_PER_RUN=1
AUTOMATION_HOUR_UTC=13
PRODUCT_IMPORT_LIMIT=25
SESSION_COOKIE_SECURE=false
''')


write("README.md", '''# eBayMarketplace.biz Product Blog

A self-publishing Flask product blog for Render. It imports eligible products from eBayMarketplace.biz, generates original buying guides, and publishes one product article per daily automation cycle.

## Publishing behavior

- `AUTO_PUBLISH=true` publishes generated articles automatically.
- The app runs a duplicate-protected cycle at startup and at the configured UTC hour.
- The product's exact name is always the visible affiliate anchor text.
- Affiliate links use `rel="sponsored noopener nofollow"` and open in a new tab.
- The product source page and marketplace homepage remain available as editorial references.

## SEO and crawling

- Every published article is immediately included in `/sitemap.xml` because the sitemap is generated live from the database.
- `/robots.txt` advertises the absolute sitemap URL.
- Each article has canonical metadata, Open Graph tags, and JSON-LD containing both `BlogPosting` and `Product` entities.
- Sitemap responses return XML with HTTP caching, ETag, and Last-Modified headers.

A valid sitemap and HTTP 200 response make the sitemap fetchable, but Google controls the Search Console status and crawl schedule. The application does not use Google OAuth or the Search Console API.

## Render configuration

The included `render.yaml` provisions one Python web service and one PostgreSQL database. Required secrets:

- `ADMIN_PASSWORD`
- `OPENAI_API_KEY` (optional; the deterministic fallback generator works without it)

Render supplies `RENDER_EXTERNAL_URL`, which the app uses automatically when `SITE_URL` is not set.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app app init-db
flask --app app run --debug
```

## Test

```bash
pytest -q
```
''')


# Regression tests for exact affiliate anchors, schema, and sitemap behavior.
write("tests/test_app.py", '''from bs4 import BeautifulSoup

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


def test_public_home_health_and_no_search_console_route():
    app = make_app()
    with app.test_client() as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert b"Marketplace Finds" in client.get("/").data
        assert client.get("/admin/gsc").status_code == 404


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


def test_self_published_article_anchor_schema_sitemap_and_audit():
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
        soup = BeautifulSoup(post.content_html, "html.parser")
        affiliate = soup.select_one(f'a[href="{product.affiliate_url}"]')
        assert affiliate is not None
        assert affiliate.get_text(" ", strip=True) == product.title
        assert "sponsored" in affiliate.get("rel", [])
        result = run_seo_audit()
        assert result["published_posts"] == 1
        post_slug = post.slug

    with app.test_client() as client:
        article = client.get(f"/blog/{post_slug}")
        assert article.status_code == 200
        assert b'"@type": "Product"' in article.data
        assert b"Example Product" in article.data
        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert sitemap.content_type.startswith("application/xml")
        assert post_slug.encode() in sitemap.data
        assert b"<lastmod>" in sitemap.data
''')


# Remove OAuth-specific files and remove this one-time migration machinery.
for relative in (
    "gsc.py",
    "templates/admin_gsc.html",
    ".github/workflows/apply-self-publish-refactor.yml",
    "scripts/refactor_no_oauth.py",
):
    path = ROOT / relative
    if path.exists():
        path.unlink()
