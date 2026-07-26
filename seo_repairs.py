from __future__ import annotations

import re
from collections import defaultdict

from bs4 import BeautifulSoup
from flask import current_app

from models import Post, SEOIssue, db, utcnow
from services import ensure_required_links, extract_product


def _word_count(html: str) -> int:
    return len(BeautifulSoup(html or "", "html.parser").get_text(" ").split())


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _upsert_issue(
    key: str,
    issue_type: str,
    details: str,
    severity: str = "warning",
    page_url: str = "",
    auto_fixable: bool = False,
) -> None:
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
        return

    db.session.add(
        SEOIssue(
            issue_key=key,
            issue_type=issue_type,
            details=details,
            severity=severity,
            page_url=page_url,
            auto_fixable=auto_fixable,
        )
    )


def _fit_meta_title(base: str, suffix: str = "") -> str:
    base = _clean_text(base) or "Shopping Guide"
    suffix = _clean_text(suffix)
    if suffix:
        suffix = f" | {suffix}"
    available = max(1, 60 - len(suffix))
    return f"{base[:available].rstrip(' -|:')}{suffix}"[:60]


def _unique_meta_title(post: Post) -> str:
    existing = {
        _clean_text(title).casefold()
        for (title,) in db.session.query(Post.meta_title)
        .filter(Post.status == "published", Post.id != post.id)
        .all()
        if _clean_text(title)
    }

    candidates = [
        _fit_meta_title(post.title),
        _fit_meta_title(post.title, post.category or "Guide"),
        _fit_meta_title(post.title, post.product.brand if post.product and post.product.brand else "Product Guide"),
        _fit_meta_title(post.title, f"Guide {post.id}"),
    ]
    for candidate in candidates:
        if candidate.casefold() not in existing:
            return candidate
    return _fit_meta_title(post.title, str(post.id))


def _truncate_description(text: str) -> str:
    text = _clean_text(text)
    if len(text) <= 160:
        return text
    shortened = text[:157].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}..."[:160]


def _build_meta_description(post: Post) -> str:
    body_text = BeautifulSoup(post.content_html or "", "html.parser").get_text(" ")
    source = _clean_text(post.excerpt) or _clean_text(post.product.description if post.product else "") or _clean_text(body_text)
    if not source:
        source = f"Review {post.title} and the main factors to compare before buying."

    description = source
    if len(description) < 70:
        description = (
            f"{description.rstrip('.')} Review key features, value, intended uses, and the current listing before buying."
        )
    if len(description) < 70:
        description = f"{description} Compare product details and available alternatives before making a decision."
    return _truncate_description(description)


def run_seo_audit() -> dict:
    active_keys: set[str] = set()
    site_url = current_app.config["SITE_URL"]
    source_home = current_app.config["SOURCE_SITE_URL"]
    posts = Post.query.filter_by(status="published").order_by(Post.id).all()

    title_groups: dict[str, list[Post]] = defaultdict(list)

    for post in posts:
        page_url = f"{site_url}/blog/{post.slug}"
        checks: list[tuple[str, str, bool]] = []

        if not _clean_text(post.meta_title) or len(post.meta_title) > 60:
            checks.append(("meta-title", "Meta title is missing or longer than 60 characters.", True))
        if not _clean_text(post.meta_description) or not 70 <= len(post.meta_description) <= 160:
            checks.append(("meta-description", "Meta description should be 70–160 characters.", True))
        if _word_count(post.content_html) < 500:
            checks.append(("thin-content", "Article contains fewer than 500 words and needs additional original value.", False))

        soup = BeautifulSoup(post.content_html or "", "html.parser")
        hrefs = [anchor.get("href", "") for anchor in soup.select("a[href]")]
        if post.product and post.product.source_url not in hrefs:
            checks.append(("missing-source-link", "The article is missing its curated eBayMarketplace.biz product-page backlink.", True))
        if post.product and post.product.affiliate_url not in hrefs:
            checks.append(("missing-affiliate-link", "The article is missing the product affiliate link.", True))
        if post.product:
            matching = [
                anchor
                for anchor in soup.select("a[href]")
                if anchor.get("href", "") == post.product.affiliate_url
            ]
            if not matching or matching[0].get_text(" ", strip=True) != post.product.title:
                checks.append(
                    (
                        "affiliate-anchor-text",
                        "The affiliate link must use the exact product name as its visible anchor text.",
                        True,
                    )
                )
        if not any(href.rstrip("/") == source_home.rstrip("/") for href in hrefs):
            checks.append(("missing-marketplace-link", "The article is missing a contextual link to eBayMarketplace.biz.", True))
        if post.product and not _clean_text(post.product.image_url):
            checks.append(
                (
                    "missing-image",
                    "The product has no image URL. Safe repair will retry the curated source page.",
                    bool(post.product.source_url),
                )
            )

        for issue_type, details, fixable in checks:
            key = f"post:{post.id}:{issue_type}"
            active_keys.add(key)
            _upsert_issue(key, issue_type, details, "warning", page_url, fixable)

        normalized_title = _clean_text(post.meta_title).casefold()
        if normalized_title:
            title_groups[normalized_title].append(post)

    for grouped_posts in title_groups.values():
        if len(grouped_posts) < 2:
            continue
        for post in grouped_posts[1:]:
            key = f"post:{post.id}:duplicate-meta-title"
            active_keys.add(key)
            _upsert_issue(
                key,
                "duplicate-meta-title",
                "This published post shares its meta title with another article. A unique title can be generated safely.",
                "warning",
                f"{site_url}/blog/{post.slug}",
                True,
            )

    for issue in SEOIssue.query.filter_by(status="open").all():
        if issue.issue_key not in active_keys:
            issue.status = "resolved"
            issue.resolved_at = utcnow()

    db.session.commit()
    return {
        "published_posts": len(posts),
        "open_issues": SEOIssue.query.filter_by(status="open").count(),
        "auto_fixable": SEOIssue.query.filter_by(status="open", auto_fixable=True).count(),
        "review_required": SEOIssue.query.filter_by(status="open", auto_fixable=False).count(),
    }


def apply_seo_fixes() -> int:
    # Refresh the audit first so stale rows cannot make the button report a misleading result.
    run_seo_audit()
    issues = SEOIssue.query.filter_by(status="open", auto_fixable=True).all()
    before_keys = {issue.issue_key for issue in issues}

    for issue in issues:
        match = re.match(r"^post:(\d+):", issue.issue_key)
        if not match:
            continue
        post = db.session.get(Post, int(match.group(1)))
        if not post:
            continue

        if issue.issue_type in {"meta-title", "duplicate-meta-title"}:
            post.meta_title = _unique_meta_title(post)
        elif issue.issue_type == "meta-description":
            post.meta_description = _build_meta_description(post)
        elif issue.issue_type in {
            "missing-source-link",
            "missing-affiliate-link",
            "affiliate-anchor-text",
            "missing-marketplace-link",
        } and post.product:
            post.content_html = ensure_required_links(post.content_html, post.product)
        elif issue.issue_type == "missing-image" and post.product and post.product.source_url:
            try:
                refreshed = extract_product(post.product.source_url)
            except Exception:
                current_app.logger.warning(
                    "Could not refresh product image for post %s", post.id, exc_info=True
                )
                refreshed = None
            if refreshed and _clean_text(refreshed.get("image_url")):
                post.product.image_url = refreshed["image_url"]
        else:
            continue

    db.session.commit()
    run_seo_audit()

    remaining_keys = {
        issue.issue_key
        for issue in SEOIssue.query.filter(
            SEOIssue.status == "open", SEOIssue.issue_key.in_(before_keys)
        ).all()
    }
    return len(before_keys - remaining_keys)
