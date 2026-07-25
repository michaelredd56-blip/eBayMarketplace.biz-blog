from __future__ import annotations

import base64
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import bleach
import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import current_app
from openai import OpenAI
from slugify import slugify
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import AutomationRun, OAuthToken, Post, Product, SEOIssue, SiteSetting, db, utcnow


USER_AGENT = "MarketplaceFindsBot/1.0 (+https://ebaymarketplace.biz)"
ALLOWED_TAGS = [
    "p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "a", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td", "figure", "figcaption", "img", "hr", "br"
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "loading", "width", "height"],
}


@dataclass
class ImportResult:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


def http_get(url: str, timeout: int = 15) -> requests.Response:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,application/json"},
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def unique_slug(model, text: str, current_id: int | None = None) -> str:
    base = slugify(text, max_length=520) or "item"
    candidate = base
    counter = 2
    while True:
        query = model.query.filter_by(slug=candidate)
        if current_id is not None:
            query = query.filter(model.id != current_id)
        if query.first() is None:
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


def setting_get(key: str, default: str = "") -> str:
    record = SiteSetting.query.filter_by(key=key).first()
    return record.value if record and record.value is not None else default


def setting_set(key: str, value: str) -> None:
    record = SiteSetting.query.filter_by(key=key).first()
    if not record:
        record = SiteSetting(key=key, value=value)
        db.session.add(record)
    else:
        record.value = value
    db.session.commit()


def _fernet() -> Fernet:
    configured = current_app.config.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if configured:
        key = configured.encode()
    else:
        digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def save_oauth_token(provider: str, payload: dict) -> None:
    encrypted = _fernet().encrypt(json.dumps(payload).encode()).decode()
    record = OAuthToken.query.filter_by(provider=provider).first()
    if record:
        record.encrypted_payload = encrypted
    else:
        db.session.add(OAuthToken(provider=provider, encrypted_payload=encrypted))
    db.session.commit()


def load_oauth_token(provider: str) -> dict | None:
    record = OAuthToken.query.filter_by(provider=provider).first()
    if not record:
        return None
    try:
        return json.loads(_fernet().decrypt(record.encrypted_payload.encode()).decode())
    except Exception:
        current_app.logger.exception("Unable to decrypt OAuth token")
        return None


def delete_oauth_token(provider: str) -> None:
    OAuthToken.query.filter_by(provider=provider).delete()
    db.session.commit()


def _parse_xml_locations(xml_text: str) -> tuple[list[str], bool]:
    soup = BeautifulSoup(xml_text, "xml")
    root = soup.find()
    is_index = bool(root and root.name and root.name.lower().endswith("sitemapindex"))
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")], is_index


def discover_source_urls(limit: int) -> list[str]:
    source = current_app.config["SOURCE_SITE_URL"]
    start = current_app.config.get("SOURCE_SITEMAP_URL") or f"{source}/sitemap.xml"
    queue = [start]
    seen_sitemaps: set[str] = set()
    pages: list[str] = []

    while queue and len(pages) < limit:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps or len(seen_sitemaps) > 20:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            response = http_get(sitemap_url)
            locations, is_index = _parse_xml_locations(response.text)
        except Exception:
            current_app.logger.warning("Could not read sitemap %s", sitemap_url, exc_info=True)
            if sitemap_url == start:
                return [source]
            continue
        if is_index:
            queue.extend(locations[:20])
        else:
            for location in locations:
                if location.startswith(source) and location not in pages:
                    pages.append(location)
                    if len(pages) >= limit:
                        break
    return pages or [source]


def _json_ld_objects(soup: BeautifulSoup) -> Iterable[dict]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict):
                        yield node
            yield item


def _is_product_schema(obj: dict) -> bool:
    kind = obj.get("@type", "")
    if isinstance(kind, list):
        return "Product" in kind
    return str(kind).lower() == "product"


def _pick_affiliate_link(soup: BeautifulSoup, base_url: str) -> str | None:
    ebay_hosts = ("ebay.com", "ebay.us", "rover.ebay.com", "ebay.to")
    candidates = []
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, anchor.get("href", "").strip())
        host = urlparse(href).netloc.lower()
        if any(domain in host for domain in ebay_hosts):
            score = 0
            lower = href.lower()
            if "campid=" in lower or "mkcid=" in lower or "customid=" in lower:
                score += 10
            text = anchor.get_text(" ", strip=True).lower()
            if any(term in text for term in ("buy", "shop", "view", "ebay", "deal")):
                score += 3
            candidates.append((score, href))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def extract_product(url: str) -> dict | None:
    response = http_get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    product_schema = next((obj for obj in _json_ld_objects(soup) if _is_product_schema(obj)), None)

    title = ""
    description = ""
    image_url = ""
    price = ""
    currency = "USD"
    category = "Featured"
    brand = ""
    affiliate_url = _pick_affiliate_link(soup, url)

    if product_schema:
        title = str(product_schema.get("name") or "").strip()
        description = str(product_schema.get("description") or "").strip()
        image = product_schema.get("image")
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, dict):
            image = image.get("url", "")
        image_url = str(image or "").strip()
        offers = product_schema.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            price = str(offers.get("price") or offers.get("lowPrice") or "").strip()
            currency = str(offers.get("priceCurrency") or "USD").strip()
            affiliate_url = affiliate_url or str(offers.get("url") or "").strip()
        category = str(product_schema.get("category") or category).strip()
        brand_value = product_schema.get("brand")
        if isinstance(brand_value, dict):
            brand_value = brand_value.get("name")
        brand = str(brand_value or "").strip()

    title = title or (soup.select_one('meta[property="og:title"]') or {}).get("content", "")
    title = title or (soup.title.get_text(strip=True) if soup.title else "")
    description = description or (soup.select_one('meta[name="description"]') or {}).get("content", "")
    image_url = image_url or (soup.select_one('meta[property="og:image"]') or {}).get("content", "")

    if not title or not affiliate_url:
        return None
    if current_app.config["SOURCE_SITE_URL"] not in url:
        return None

    return {
        "title": re.sub(r"\s+", " ", title).strip()[:500],
        "source_url": url,
        "affiliate_url": affiliate_url,
        "image_url": image_url,
        "price": price[:80],
        "currency": currency[:8],
        "category": category[:180] or "Featured",
        "description": re.sub(r"\s+", " ", BeautifulSoup(description, "html.parser").get_text(" ")).strip(),
        "brand": brand[:180],
    }


def upsert_product(data: dict) -> str:
    product = Product.query.filter_by(source_url=data["source_url"]).first()
    if product:
        for field in ("title", "affiliate_url", "image_url", "price", "currency", "category", "description", "brand"):
            if data.get(field):
                setattr(product, field, data[field])
        product.active = True
        return "updated"
    product = Product(**data, slug=unique_slug(Product, data["title"]))
    db.session.add(product)
    return "created"


def import_products(limit: int | None = None) -> ImportResult:
    limit = limit or current_app.config["PRODUCT_IMPORT_LIMIT"]
    result = ImportResult()
    urls = discover_source_urls(limit * 4)
    random.shuffle(urls)
    for url in urls:
        if result.created + result.updated >= limit:
            break
        result.scanned += 1
        try:
            data = extract_product(url)
            if not data:
                result.skipped += 1
                continue
            action = upsert_product(data)
            setattr(result, action, getattr(result, action) + 1)
            db.session.commit()
        except Exception:
            db.session.rollback()
            result.errors += 1
            current_app.logger.warning("Product import failed for %s", url, exc_info=True)
    return result


def add_manual_product(data: dict) -> Product:
    title = data.get("title", "").strip()
    source_url = data.get("source_url", "").strip()
    affiliate_url = data.get("affiliate_url", "").strip()
    if not title or not source_url or not affiliate_url:
        raise ValueError("Title, marketplace page URL, and affiliate URL are required.")
    for field_url in (source_url, affiliate_url):
        if urlparse(field_url).scheme not in {"http", "https"}:
            raise ValueError("Product URLs must begin with http:// or https://.")
    product = Product(
        title=title[:500],
        slug=unique_slug(Product, title),
        source_url=source_url,
        affiliate_url=affiliate_url,
        image_url=data.get("image_url", "").strip(),
        price=data.get("price", "").strip()[:80],
        currency=data.get("currency", "USD").strip()[:8] or "USD",
        category=data.get("category", "Featured").strip()[:180] or "Featured",
        description=data.get("description", "").strip(),
        brand=data.get("brand", "").strip()[:180],
    )
    db.session.add(product)
    db.session.commit()
    return product


def select_product_for_article(product_id: int | None = None) -> Product:
    if product_id:
        product = db.session.get(Product, product_id)
        if not product or not product.active:
            raise ValueError("The selected product is unavailable.")
        return product

    cutoff = utcnow() - timedelta(days=90)
    recently_used_ids = (
        db.session.query(Post.product_id)
        .filter(Post.product_id.isnot(None), Post.created_at >= cutoff)
        .subquery()
    )
    candidates = Product.query.filter(Product.active.is_(True), ~Product.id.in_(recently_used_ids)).all()
    if not candidates:
        candidates = Product.query.filter_by(active=True).all()
    if not candidates:
        raise ValueError("No active products are available. Import or add products first.")
    return random.choice(candidates)


def _marketplace_anchor(category: str) -> str:
    options = [
        "browse more marketplace finds",
        "discover more online deals",
        f"explore more {category.lower()} products",
        "shop additional product selections",
        "see more useful shopping ideas",
    ]
    return random.choice(options)


def _fallback_article(product: Product) -> dict:
    source_site = current_app.config["SOURCE_SITE_URL"]
    title = f"Is {product.title} Worth Considering? A Practical Buying Guide"
    keyword = product.title[:120]
    description = product.description or (
        f"This guide examines the main buying considerations for {product.title}, including use cases, features, and value."
    )
    anchor = _marketplace_anchor(product.category or "featured")
    content = f"""
<p>Shopping for <strong>{escape(product.title)}</strong> is easier when you know which details matter before purchasing. This guide summarizes the product, the type of shopper it may suit, and the practical questions to ask before following the listing.</p>
<h2>Product overview</h2>
<p>{escape(description)}</p>
<h2>What to evaluate before buying</h2>
<ul>
  <li><strong>Fit for your needs:</strong> Compare the listed features with how you expect to use the product.</li>
  <li><strong>Condition and seller details:</strong> Review the current listing, photographs, shipping terms, and return information.</li>
  <li><strong>Total cost:</strong> Consider the item price together with shipping, taxes, accessories, and possible replacement parts.</li>
  <li><strong>Alternatives:</strong> Compare similar listings before deciding whether this option offers the best overall value.</li>
</ul>
<h2>Who may find it useful</h2>
<p>This product may appeal to shoppers who want a straightforward option in the {escape(product.category or 'featured')} category. The final decision should be based on the live listing because availability, price, condition, and shipping terms can change.</p>
<h2>Where to view the product</h2>
<p>Review the original curated page on <a href="{escape(product.source_url)}" rel="noopener">eBayMarketplace.biz</a>, or <a href="{escape(product.affiliate_url)}" target="_blank" rel="sponsored noopener nofollow">view the current eBay listing</a>.</p>
<p>You can also <a href="{escape(source_site)}">{escape(anchor)}</a> on eBayMarketplace.biz.</p>
<h2>Final buying checklist</h2>
<ol>
  <li>Confirm the exact model, size, color, or configuration.</li>
  <li>Check seller feedback and the complete item description.</li>
  <li>Review shipping time, return terms, and warranty information.</li>
  <li>Compare at least two similar products before completing the purchase.</li>
</ol>
<p><em>Affiliate disclosure: Some links may be affiliate links. The publisher may earn a commission from qualifying purchases at no additional cost to you.</em></p>
"""
    return {
        "title": title,
        "excerpt": f"A practical overview of {product.title}, including features to review, ideal use cases, and buying considerations.",
        "content_html": content,
        "meta_title": title[:60],
        "meta_description": f"Review {product.title}, key buying considerations, use cases, and the current product listing."[:160],
        "focus_keyword": keyword,
        "category": product.category or "Shopping Guides",
    }


def _ai_article(product: Product) -> dict:
    api_key = current_app.config["OPENAI_API_KEY"]
    if not api_key:
        return _fallback_article(product)
    client = OpenAI(api_key=api_key)
    source_site = current_app.config["SOURCE_SITE_URL"]
    prompt = f"""
Create an original, useful, non-deceptive product buying guide in valid JSON. Do not claim personal testing, guarantees, discounts, exact availability, or seller facts that were not provided. Do not copy listing text. Write 800-1,200 words in semantic HTML using p, h2, h3, ul, ol, li, strong, em, and a tags.

Product title: {product.title}
Brand: {product.brand or 'Not provided'}
Category: {product.category or 'Featured'}
Price shown by source: {product.price or 'Not provided'} {product.currency or ''}
Description/source notes: {product.description or 'No description supplied'}
Curated source page: {product.source_url}
Affiliate product URL: {product.affiliate_url}
Marketplace homepage: {source_site}

Requirements:
- Include a balanced overview, who it may suit, key features to verify, practical use cases, drawbacks/limitations to consider, comparison questions, and a final checklist.
- Include one natural editorial backlink to the curated source page.
- Include one clear call-to-action link to the affiliate product URL with rel="sponsored noopener nofollow" and target="_blank".
- Include one natural contextual link to the marketplace homepage using varied anchor text, not an exact-match stuffed keyword.
- Include an affiliate disclosure at the end.
- Avoid medical, financial, safety, or performance guarantees.
- Return only JSON with: title, excerpt, content_html, meta_title, meta_description, focus_keyword, category.
- meta_title must be <= 60 characters; meta_description <= 160 characters.
"""
    response = client.responses.create(
        model=current_app.config["OPENAI_MODEL"],
        input=prompt,
        temperature=0.7,
    )
    text = response.output_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    data = json.loads(text)
    required = {"title", "excerpt", "content_html", "meta_title", "meta_description", "focus_keyword", "category"}
    if not required.issubset(data):
        raise ValueError("AI response was missing required article fields")
    return data


def sanitize_article_html(html: str) -> str:
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    soup = BeautifulSoup(cleaned, "html.parser")
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        host = urlparse(href).netloc.lower()
        if "ebay." in host or "rover.ebay.com" in host:
            anchor["rel"] = "sponsored noopener nofollow"
            anchor["target"] = "_blank"
        elif host:
            anchor["rel"] = "noopener"
    for image in soup.select("img"):
        image["loading"] = "lazy"
    return str(soup)


def ensure_required_links(content_html: str, product: Product) -> str:
    soup = BeautifulSoup(content_html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.select("a[href]")]
    additions = []
    if product.source_url not in hrefs:
        additions.append(f'<p>See the curated product page on <a href="{escape(product.source_url)}">eBayMarketplace.biz</a>.</p>')
    if product.affiliate_url not in hrefs:
        additions.append(f'<p><a href="{escape(product.affiliate_url)}" target="_blank" rel="sponsored noopener nofollow">View the current product listing</a>.</p>')
    source_home = current_app.config["SOURCE_SITE_URL"]
    if not any(h.rstrip("/") == source_home.rstrip("/") for h in hrefs):
        additions.append(f'<p>Visit <a href="{escape(source_home)}">eBayMarketplace.biz</a> to browse more product selections.</p>')
    return content_html + "\n" + "\n".join(additions)


def generate_post(product_id: int | None = None, publish: bool | None = None) -> Post:
    product = select_product_for_article(product_id)
    try:
        data = _ai_article(product)
    except Exception:
        current_app.logger.exception("AI generation failed; using deterministic fallback")
        data = _fallback_article(product)

    title = str(data["title"]).strip()[:500]
    content_html = sanitize_article_html(str(data["content_html"]))
    content_html = ensure_required_links(content_html, product)
    status = "published" if (current_app.config["AUTO_PUBLISH"] if publish is None else publish) else "draft"
    post = Post(
        product=product,
        title=title,
        slug=unique_slug(Post, title),
        excerpt=str(data["excerpt"]).strip()[:1000],
        content_html=content_html,
        meta_title=str(data["meta_title"]).strip()[:60] or title[:60],
        meta_description=str(data["meta_description"]).strip()[:160],
        focus_keyword=str(data["focus_keyword"]).strip()[:255],
        category=str(data["category"]).strip()[:180] or product.category or "Shopping Guides",
        status=status,
        auto_generated=True,
        published_at=utcnow() if status == "published" else None,
    )
    db.session.add(post)
    db.session.commit()
    return post


def publish_post(post: Post) -> None:
    post.status = "published"
    post.published_at = post.published_at or utcnow()
    db.session.commit()


def update_post(post: Post, data: dict) -> None:
    old_title = post.title
    post.title = data.get("title", post.title).strip()[:500]
    post.excerpt = data.get("excerpt", post.excerpt).strip()[:1000]
    post.content_html = sanitize_article_html(data.get("content_html", post.content_html))
    if post.product:
        post.content_html = ensure_required_links(post.content_html, post.product)
    post.meta_title = data.get("meta_title", post.meta_title).strip()[:60]
    post.meta_description = data.get("meta_description", post.meta_description).strip()[:160]
    post.focus_keyword = data.get("focus_keyword", post.focus_keyword or "").strip()[:255]
    post.category = data.get("category", post.category).strip()[:180]
    if post.title != old_title and data.get("regenerate_slug"):
        post.slug = unique_slug(Post, post.title, post.id)
    db.session.commit()


def _word_count(html: str) -> int:
    return len(BeautifulSoup(html, "html.parser").get_text(" ").split())


def _upsert_issue(key: str, issue_type: str, details: str, severity: str = "warning", page_url: str = "", auto_fixable: bool = False) -> None:
    issue = SEOIssue.query.filter_by(issue_key=key).first()
    if issue:
        issue.issue_type = issue_type
        issue.details = details
        issue.severity = severity
        issue.page_url = page_url
        issue.auto_fixable = auto_fixable
        issue.status = "open"
        issue.detected_at = utcnow()
        issue.resolved_at = None
    else:
        db.session.add(SEOIssue(
            issue_key=key,
            issue_type=issue_type,
            details=details,
            severity=severity,
            page_url=page_url,
            auto_fixable=auto_fixable,
        ))


def run_seo_audit() -> dict:
    active_keys: set[str] = set()
    site_url = current_app.config["SITE_URL"]
    source_home = current_app.config["SOURCE_SITE_URL"]
    posts = Post.query.filter_by(status="published").all()

    for post in posts:
        page_url = f"{site_url}/blog/{post.slug}"
        checks = []
        if not post.meta_title or len(post.meta_title) > 60:
            checks.append(("meta-title", "Meta title is missing or longer than 60 characters.", True))
        if not post.meta_description or not 70 <= len(post.meta_description) <= 160:
            checks.append(("meta-description", "Meta description should usually be 70–160 characters.", True))
        if _word_count(post.content_html) < 500:
            checks.append(("thin-content", "Article contains fewer than 500 words and may need more original value.", False))
        soup = BeautifulSoup(post.content_html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.select("a[href]")]
        if post.product and post.product.source_url not in hrefs:
            checks.append(("missing-source-link", "The article is missing its curated eBayMarketplace.biz product-page backlink.", True))
        if post.product and post.product.affiliate_url not in hrefs:
            checks.append(("missing-affiliate-link", "The article is missing the product affiliate link.", True))
        if not any(h.rstrip("/") == source_home.rstrip("/") for h in hrefs):
            checks.append(("missing-marketplace-link", "The article is missing a contextual link to eBayMarketplace.biz.", True))
        if post.product and not post.product.image_url:
            checks.append(("missing-image", "The linked product has no image URL.", False))

        for issue_type, details, fixable in checks:
            key = f"post:{post.id}:{issue_type}"
            active_keys.add(key)
            _upsert_issue(key, issue_type, details, "warning", page_url, fixable)

    duplicate_titles = (
        db.session.query(Post.meta_title, func.count(Post.id))
        .filter(Post.status == "published")
        .group_by(Post.meta_title)
        .having(func.count(Post.id) > 1)
        .all()
    )
    for title, count in duplicate_titles:
        key = "duplicate-title:" + hashlib.sha1((title or "blank").encode()).hexdigest()
        active_keys.add(key)
        _upsert_issue(key, "duplicate-meta-title", f'{count} published posts share the meta title "{title}".', "warning", auto_fixable=False)

    open_issues = SEOIssue.query.filter_by(status="open").all()
    for issue in open_issues:
        if issue.issue_key not in active_keys:
            issue.status = "resolved"
            issue.resolved_at = utcnow()
    db.session.commit()
    return {
        "published_posts": len(posts),
        "open_issues": SEOIssue.query.filter_by(status="open").count(),
        "auto_fixable": SEOIssue.query.filter_by(status="open", auto_fixable=True).count(),
    }


def apply_seo_fixes() -> int:
    fixed = 0
    issues = SEOIssue.query.filter_by(status="open", auto_fixable=True).all()
    for issue in issues:
        match = re.match(r"post:(\d+):", issue.issue_key)
        if not match:
            continue
        post = db.session.get(Post, int(match.group(1)))
        if not post:
            continue
        if issue.issue_type == "meta-title":
            post.meta_title = post.title[:60]
        elif issue.issue_type == "meta-description":
            text = post.excerpt or BeautifulSoup(post.content_html, "html.parser").get_text(" ")
            post.meta_description = re.sub(r"\s+", " ", text).strip()[:157].rstrip(" ,;:") + "..."
        elif issue.issue_type in {"missing-source-link", "missing-affiliate-link", "missing-marketplace-link"} and post.product:
            post.content_html = ensure_required_links(post.content_html, post.product)
        else:
            continue
        issue.status = "resolved"
        issue.resolved_at = utcnow()
        fixed += 1
    db.session.commit()
    run_seo_audit()
    return fixed


def run_automation_cycle(force: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    run_key = f"daily:{now.date().isoformat()}"
    if force:
        run_key = f"manual:{now.isoformat()}"
    run = AutomationRun(run_key=run_key)
    db.session.add(run)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return {"status": "skipped", "reason": "Automation already ran for this date."}

    details: dict = {"import": None, "posts": [], "audit": None}
    try:
        imported = import_products()
        details["import"] = imported.__dict__
        for _ in range(current_app.config["POSTS_PER_RUN"]):
            try:
                post = generate_post()
                details["posts"].append({"id": post.id, "title": post.title, "status": post.status})
            except ValueError as exc:
                details["posts"].append({"error": str(exc)})
                break
        details["audit"] = run_seo_audit()
        details["search_console"] = sync_search_console_signals()
        run.status = "completed"
    except Exception as exc:
        current_app.logger.exception("Automation cycle failed")
        run.status = "failed"
        details["error"] = str(exc)
    run.details = json.dumps(details)
    run.finished_at = utcnow()
    db.session.commit()
    return {"status": run.status, **details}


def sync_search_console_signals() -> dict:
    """Convert actionable Search Console signals into the same SEO issue queue."""
    if OAuthToken.query.filter_by(provider="google_search_console").first() is None:
        return {"status": "not-connected", "issues": 0}
    import gsc

    active_keys: set[str] = set()
    created_or_updated = 0
    try:
        for row in gsc.performance_by_page(days=28, row_limit=100):
            page = (row.get("keys") or [""])[0]
            impressions = float(row.get("impressions", 0))
            ctr = float(row.get("ctr", 0))
            if impressions >= 100 and ctr < 0.01:
                key = "gsc-low-ctr:" + hashlib.sha1(page.encode()).hexdigest()
                active_keys.add(key)
                _upsert_issue(
                    key,
                    "search-console-low-ctr",
                    f"Google Search Console reports {impressions:.0f} impressions and {ctr * 100:.1f}% CTR over the latest available 28-day period. Review the title, description, and search intent before changing content.",
                    "warning",
                    page,
                    False,
                )
                created_or_updated += 1
        for sitemap in gsc.list_sitemaps():
            errors = int(sitemap.get("errors", 0) or 0)
            warnings = int(sitemap.get("warnings", 0) or 0)
            if errors or warnings:
                path = sitemap.get("path", "sitemap")
                key = "gsc-sitemap:" + hashlib.sha1(path.encode()).hexdigest()
                active_keys.add(key)
                _upsert_issue(
                    key,
                    "search-console-sitemap",
                    f"Search Console reports {errors} sitemap errors and {warnings} warnings for {path}.",
                    "error" if errors else "warning",
                    path,
                    False,
                )
                created_or_updated += 1
        db.session.commit()
        return {"status": "completed", "issues": created_or_updated}
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Search Console signal sync failed")
        return {"status": "failed", "error": str(exc), "issues": 0}


def record_url_inspection(url: str, result: dict) -> dict:
    inspection = result.get("inspectionResult", {})
    index = inspection.get("indexStatusResult", {})
    verdict = index.get("verdict", "VERDICT_UNSPECIFIED")
    coverage = index.get("coverageState", "Unknown")
    key = "gsc-inspection:" + hashlib.sha1(url.encode()).hexdigest()
    if verdict == "PASS":
        issue = SEOIssue.query.filter_by(issue_key=key).first()
        if issue:
            issue.status = "resolved"
            issue.resolved_at = utcnow()
        db.session.commit()
    else:
        _upsert_issue(
            key,
            "search-console-indexing",
            f"URL Inspection verdict: {verdict}. Coverage state: {coverage}. Review canonical, robots, sitemap inclusion, content quality, and crawl accessibility.",
            "warning",
            url,
            False,
        )
        db.session.commit()
    return {"verdict": verdict, "coverage": coverage}
