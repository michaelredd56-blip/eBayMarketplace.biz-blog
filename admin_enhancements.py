from __future__ import annotations

import re

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import desc

from models import Post, SEOIssue, db
from seo_repairs import apply_seo_fixes, run_seo_audit
from services import publish_post


def _post_id_from_issue(issue: SEOIssue) -> int | None:
    match = re.match(r"^post:(\d+):", issue.issue_key or "")
    return int(match.group(1)) if match else None


def install_admin_enhancements(app) -> None:
    """Install production admin routes without changing the public URL structure."""
    from app import _safe_next_url, admin_required

    def enhanced_admin_seo():
        # Keep the page accurate even when old database rows were created by an earlier audit version.
        run_seo_audit()
        issues = (
            SEOIssue.query.filter_by(status="open")
            .order_by(SEOIssue.severity, desc(SEOIssue.detected_at))
            .all()
        )
        post_ids = {_post_id_from_issue(issue) for issue in issues}
        post_ids.discard(None)
        posts = Post.query.filter(Post.id.in_(post_ids)).all() if post_ids else []
        posts_by_id = {post.id: post for post in posts}
        issue_rows = [
            {"issue": issue, "post": posts_by_id.get(_post_id_from_issue(issue))}
            for issue in issues
        ]
        review_queue = (
            Post.query.filter_by(status="draft")
            .order_by(desc(Post.updated_at), desc(Post.id))
            .limit(100)
            .all()
        )
        return render_template(
            "admin_seo.html",
            issues=issues,
            issue_rows=issue_rows,
            review_queue=review_queue,
        )

    def enhanced_admin_seo_audit():
        result = run_seo_audit()
        flash(
            f"Audit completed. {result['open_issues']} open findings; "
            f"{result['auto_fixable']} safe automatic fixes; "
            f"{result['review_required']} need review.",
            "success",
        )
        return redirect(url_for("admin_seo"))

    def enhanced_admin_seo_fix():
        report = apply_seo_fixes()
        if report["applied"]:
            category = "success" if not report["unresolved"] else "warning"
            message = f"Applied {report['applied']} of {report['attempted']} safe SEO fixes."
            if report["unresolved"]:
                message += " Remaining findings were left open for review."
            flash(message, category)
        elif report["attempted"]:
            flash(
                "No automatic changes passed the follow-up audit. The remaining findings "
                "have been left open for manual review instead of being counted as repaired.",
                "warning",
            )
        else:
            flash("There are currently no safe automatic fixes to apply.", "warning")
        return redirect(url_for("admin_seo"))

    def enhanced_admin_post_publish(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        publish_post(post)
        run_seo_audit()
        flash(f'Published “{post.title}”.', "success")
        return redirect(_safe_next_url(request.form.get("next")) or url_for("admin_posts"))

    def enhanced_admin_post_unpublish(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        post.status = "draft"
        db.session.commit()
        run_seo_audit()
        flash(f'Moved “{post.title}” to drafts for review.', "success")
        return redirect(_safe_next_url(request.form.get("next")) or url_for("admin_posts"))

    app.view_functions["admin_seo"] = admin_required(enhanced_admin_seo)
    app.view_functions["admin_seo_audit"] = admin_required(enhanced_admin_seo_audit)
    app.view_functions["admin_seo_fix"] = admin_required(enhanced_admin_seo_fix)
    app.view_functions["admin_post_publish"] = admin_required(enhanced_admin_post_publish)
    app.view_functions["admin_post_unpublish"] = admin_required(enhanced_admin_post_unpublish)

    if "admin_post_delete" not in app.view_functions:
        @app.post("/admin/posts/<int:post_id>/delete")
        @admin_required
        def admin_post_delete(post_id: int):
            post = db.session.get(Post, post_id) or abort(404)
            title = post.title
            SEOIssue.query.filter(
                SEOIssue.issue_key.like(f"post:{post_id}:%")
            ).delete(synchronize_session=False)
            db.session.delete(post)
            db.session.commit()
            flash(f'Deleted “{title}”.', "success")
            return redirect(_safe_next_url(request.form.get("next")) or url_for("admin_posts"))
