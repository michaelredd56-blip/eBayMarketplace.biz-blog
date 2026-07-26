from __future__ import annotations

import re

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import desc

from content_enhancements import ensure_post_product_image, valid_image_url
from models import Post, SEOIssue, db
from seo_repairs import apply_seo_fixes, run_seo_audit
from services import publish_post, update_post


def _post_id_from_issue(issue: SEOIssue) -> int | None:
    match = re.match(r"^post:(\d+):", issue.issue_key or "")
    return int(match.group(1)) if match else None


def install_admin_enhancements(app) -> None:
    """Install production admin routes without changing the public URL structure."""
    from app import _safe_next_url, admin_required

    def enhanced_admin_seo():
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

    def enhanced_admin_post_edit(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        return_to = _safe_next_url(request.args.get("next") or request.form.get("next"))
        return_to = return_to or url_for("admin_posts")

        if request.method == "POST":
            image_url = request.form.get("product_image_url", "").strip()
            if image_url and not valid_image_url(image_url):
                flash("The product image URL must begin with http:// or https://.", "danger")
                return render_template("admin_post_edit.html", post=post, return_to=return_to), 400

            try:
                update_post(post, request.form)
                if post.product:
                    post.product.image_url = image_url or None
                    if not post.product.image_url:
                        ensure_post_product_image(post)
                    db.session.commit()

                if request.form.get("action") == "publish":
                    image_added = ensure_post_product_image(post)
                    publish_post(post)
                    run_seo_audit()
                    flash(f'Published “{post.title}” live on the website.', "success")
                    if not post.product or not post.product.image_url:
                        flash(
                            "The article is live, but no verified product image was available. "
                            "Add the correct image URL from the product source when available.",
                            "warning",
                        )
                    elif image_added:
                        flash("A verified product image was added before publication.", "success")
                    return redirect(return_to)

                flash("Article changes saved.", "success")
                return redirect(url_for("admin_post_edit", post_id=post.id, next=return_to))
            except Exception as exc:
                db.session.rollback()
                app.logger.exception("Could not save reviewed article")
                flash(f"Could not save the article: {exc}", "danger")

        if ensure_post_product_image(post):
            db.session.commit()
        return render_template("admin_post_edit.html", post=post, return_to=return_to)

    def enhanced_admin_post_publish(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        was_published = post.status == "published"
        image_added = ensure_post_product_image(post)
        publish_post(post)
        run_seo_audit()
        action = "Republished" if was_published else "Published"
        flash(f'{action} “{post.title}” live on the website.', "success")
        if not post.product or not post.product.image_url:
            flash(
                "No verified product image was available for this article. "
                "Use Review Content to add the correct product image URL.",
                "warning",
            )
        elif image_added:
            flash("A verified product image was added before publication.", "success")
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
    app.view_functions["admin_post_edit"] = admin_required(enhanced_admin_post_edit)
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
