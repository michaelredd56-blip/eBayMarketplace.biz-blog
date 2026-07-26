from __future__ import annotations

import argparse
import hmac
import threading
import os
import secrets
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import desc, func
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from config import Config
from models import AutomationRun, Post, Product, SEOIssue, db, utcnow
from services import (
    add_manual_product,
    apply_seo_fixes,
    generate_post,
    import_products,
    publish_post,
    run_automation_cycle,
    run_seo_audit,
    update_post,
)


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_hooks(app)
    register_routes(app)
    register_cli(app)
    start_scheduler(app)
    return app


def register_hooks(app: Flask) -> None:
    @app.before_request
    def session_and_csrf():
        session.permanent = True
        if request.method == "POST":
            expected = session.get("csrf_token")
            received = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not received or not hmac.compare_digest(expected, received):
                abort(400, description="Invalid or missing CSRF token.")

    @app.after_request
    def security_headers(response: Response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self';",
        )
        if request.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def inject_globals():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {
            "csrf_token": session["csrf_token"],
            "site_name": app.config["SITE_NAME"],
            "site_tagline": app.config["SITE_TAGLINE"],
            "site_url": app.config["SITE_URL"],
            "source_site_url": app.config["SOURCE_SITE_URL"],
            "current_year": datetime.now(timezone.utc).year,
            "is_admin": bool(session.get("admin_authenticated")),
        }

    @app.template_filter("money")
    def money_filter(value, currency="USD"):
        if value in (None, ""):
            return ""
        try:
            amount = float(value)
            symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$"}
            return f"{symbols.get(currency, currency + ' ')}{amount:,.2f}"
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("datefmt")
    def date_filter(value, fmt="%B %d, %Y"):
        return value.strftime(fmt) if value else ""

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", title="Page not found", message="The requested page could not be found."), 404

    @app.errorhandler(500)
    def server_error(_):
        db.session.rollback()
        return render_template("error.html", title="Something went wrong", message="The site encountered an unexpected error."), 500


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            flash("Please sign in to access the admin area.", "warning")
            return redirect(url_for("admin_login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def _safe_next_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.netloc or parsed.scheme:
        return None
    return value if value.startswith("/") else None


def _admin_password_valid(candidate: str, app: Flask) -> bool:
    configured_hash = app.config.get("ADMIN_PASSWORD_HASH", "")
    configured_plain = app.config.get("ADMIN_PASSWORD", "")
    if configured_hash:
        return check_password_hash(configured_hash, candidate)
    if configured_plain:
        return hmac.compare_digest(configured_plain, candidate)
    return False


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        page = max(request.args.get("page", 1, type=int), 1)
        posts = (
            Post.query.filter_by(status="published")
            .order_by(desc(Post.published_at), desc(Post.id))
            .paginate(page=page, per_page=9, error_out=False)
        )
        featured_products = Product.query.filter_by(active=True).order_by(desc(Product.updated_at)).limit(6).all()
        categories = (
            db.session.query(Post.category, func.count(Post.id))
            .filter(Post.status == "published")
            .group_by(Post.category)
            .order_by(func.count(Post.id).desc())
            .limit(8)
            .all()
        )
        return render_template("index.html", posts=posts, featured_products=featured_products, categories=categories)

    @app.get("/blog/<slug>")
    def article(slug: str):
        post = Post.query.filter_by(slug=slug, status="published").first_or_404()
        related = (
            Post.query.filter(Post.status == "published", Post.id != post.id, Post.category == post.category)
            .order_by(desc(Post.published_at))
            .limit(3)
            .all()
        )
        canonical = url_for("article", slug=post.slug, _external=True)
        blog_post = {
            "@type": "BlogPosting",
            "headline": post.title,
            "description": post.meta_description,
            "url": canonical,
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            "datePublished": post.published_at.isoformat() if post.published_at else "",
            "dateModified": post.updated_at.isoformat(),
            "publisher": {
                "@type": "Organization",
                "name": app.config["SITE_NAME"],
                "url": request.url_root.rstrip("/"),
            },
        }
        graph = [blog_post]
        if post.product:
            product_id = canonical + "#product"
            product_schema = {
                "@type": "Product",
                "@id": product_id,
                "name": post.product.title,
                "description": post.product.description or post.excerpt,
                "url": post.product.affiliate_url,
                "offers": {"@type": "Offer", "url": post.product.affiliate_url},
            }
            if post.product.image_url:
                product_schema["image"] = [post.product.image_url]
                blog_post["image"] = [post.product.image_url]
            if post.product.brand:
                product_schema["brand"] = {"@type": "Brand", "name": post.product.brand}
            if post.product.price:
                product_schema["offers"]["price"] = post.product.price
                product_schema["offers"]["priceCurrency"] = post.product.currency or "USD"
            blog_post["mainEntity"] = {"@id": product_id}
            graph.append(product_schema)
        article_schema = {"@context": "https://schema.org", "@graph": graph}
        return render_template(
            "article.html", post=post, related=related, canonical=canonical, article_schema=article_schema
        )

    @app.get("/category/<path:category>")
    def category(category: str):
        page = max(request.args.get("page", 1, type=int), 1)
        posts = (
            Post.query.filter(Post.status == "published", func.lower(Post.category) == category.replace("-", " ").lower())
            .order_by(desc(Post.published_at))
            .paginate(page=page, per_page=9, error_out=False)
        )
        return render_template("listing.html", title=category.replace("-", " ").title(), posts=posts)

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        page = max(request.args.get("page", 1, type=int), 1)
        base = Post.query.filter_by(status="published")
        if query:
            like = f"%{query}%"
            base = base.filter(
                Post.title.ilike(like) | Post.excerpt.ilike(like) | Post.focus_keyword.ilike(like)
            )
        posts = base.order_by(desc(Post.published_at)).paginate(page=page, per_page=9, error_out=False)
        return render_template("listing.html", title=f'Search results for “{query}”' if query else "Search", posts=posts, query=query)

    @app.get("/about")
    def about():
        return render_template("page.html", title="About", page_key="about")

    @app.get("/privacy")
    def privacy():
        return render_template("page.html", title="Privacy Policy", page_key="privacy")

    @app.get("/affiliate-disclosure")
    def affiliate_disclosure():
        return render_template("page.html", title="Affiliate Disclosure", page_key="affiliate")

    @app.get("/robots.txt")
    def robots():
        body = f"User-agent: *\nAllow: /\nDisallow: /admin/\nSitemap: {url_for('sitemap', _external=True)}\n"
        return Response(body, mimetype="text/plain")

    @app.get("/sitemap.xml")
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

    @app.get("/feed.xml")
    def feed():
        posts = Post.query.filter_by(status="published").order_by(desc(Post.published_at)).limit(25).all()
        return Response(render_template("feed.xml", posts=posts), mimetype="application/rss+xml")

    @app.get("/healthz")
    def healthz():
        try:
            db.session.execute(db.select(func.count(Product.id))).scalar()
            return {"status": "ok", "database": "ok"}, 200
        except Exception:
            app.logger.exception("Health check failed")
            return {"status": "error", "database": "unavailable"}, 503

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if hmac.compare_digest(username, app.config["ADMIN_USERNAME"]) and _admin_password_valid(password, app):
                session.clear()
                session["admin_authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                flash("Signed in successfully.", "success")
                return redirect(_safe_next_url(request.form.get("next")) or url_for("admin_dashboard"))
            flash("Incorrect username or password.", "danger")
        return render_template("admin_login.html", next_url=_safe_next_url(request.args.get("next")))

    @app.post("/admin/logout")
    @admin_required
    def admin_logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        stats = {
            "products": Product.query.count(),
            "active_products": Product.query.filter_by(active=True).count(),
            "published_posts": Post.query.filter_by(status="published").count(),
            "draft_posts": Post.query.filter_by(status="draft").count(),
            "seo_issues": SEOIssue.query.filter_by(status="open").count(),
        }
        recent_posts = Post.query.order_by(desc(Post.created_at)).limit(7).all()
        recent_runs = AutomationRun.query.order_by(desc(AutomationRun.started_at)).limit(5).all()
        return render_template("admin_dashboard.html", stats=stats, recent_posts=recent_posts, recent_runs=recent_runs)

    @app.route("/admin/products", methods=["GET", "POST"])
    @admin_required
    def admin_products():
        if request.method == "POST":
            try:
                product = add_manual_product(request.form)
                flash(f'Added “{product.title}”.', "success")
                return redirect(url_for("admin_products"))
            except Exception as exc:
                db.session.rollback()
                flash(str(exc), "danger")
        products = Product.query.order_by(desc(Product.updated_at)).limit(250).all()
        return render_template("admin_products.html", products=products)

    @app.post("/admin/products/import")
    @admin_required
    def admin_products_import():
        limit = min(max(request.form.get("limit", app.config["PRODUCT_IMPORT_LIMIT"], type=int), 1), 250)
        result = import_products(limit)
        flash(
            f"Import finished: {result.created} created, {result.updated} updated, "
            f"{result.skipped} skipped, {result.errors} errors.",
            "success" if result.errors == 0 else "warning",
        )
        return redirect(url_for("admin_products"))

    @app.post("/admin/products/<int:product_id>/toggle")
    @admin_required
    def admin_product_toggle(product_id: int):
        product = db.session.get(Product, product_id) or abort(404)
        product.active = not product.active
        db.session.commit()
        flash(f'“{product.title}” is now {"active" if product.active else "inactive"}.', "success")
        return redirect(url_for("admin_products"))

    @app.get("/admin/posts")
    @admin_required
    def admin_posts():
        posts = Post.query.order_by(desc(Post.created_at)).limit(250).all()
        products = Product.query.filter_by(active=True).order_by(Product.title).all()
        return render_template("admin_posts.html", posts=posts, products=products)

    @app.post("/admin/posts/generate")
    @admin_required
    def admin_posts_generate():
        product_id = request.form.get("product_id", type=int)
        publish = request.form.get("publish") == "1"
        try:
            post = generate_post(product_id, publish=publish)
            flash(f'Generated “{post.title}” as {post.status}.', "success")
            return redirect(url_for("admin_post_edit", post_id=post.id))
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("Post generation failed")
            flash(f"Could not generate the article: {exc}", "danger")
            return redirect(url_for("admin_posts"))

    @app.route("/admin/posts/<int:post_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_post_edit(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        if request.method == "POST":
            try:
                update_post(post, request.form)
                flash("Post saved.", "success")
                return redirect(url_for("admin_post_edit", post_id=post.id))
            except Exception as exc:
                db.session.rollback()
                flash(f"Could not save the post: {exc}", "danger")
        return render_template("admin_post_edit.html", post=post)

    @app.post("/admin/posts/<int:post_id>/publish")
    @admin_required
    def admin_post_publish(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        publish_post(post)
        flash(f'Published “{post.title}”.', "success")
        return redirect(url_for("admin_posts"))

    @app.post("/admin/posts/<int:post_id>/unpublish")
    @admin_required
    def admin_post_unpublish(post_id: int):
        post = db.session.get(Post, post_id) or abort(404)
        post.status = "draft"
        db.session.commit()
        flash(f'Moved “{post.title}” to drafts.', "success")
        return redirect(url_for("admin_posts"))

    @app.get("/admin/seo")
    @admin_required
    def admin_seo():
        issues = SEOIssue.query.filter_by(status="open").order_by(SEOIssue.severity, desc(SEOIssue.detected_at)).all()
        return render_template("admin_seo.html", issues=issues)

    @app.post("/admin/seo/audit")
    @admin_required
    def admin_seo_audit():
        result = run_seo_audit()
        flash(
            f"Audit completed. {result['open_issues']} open issues; "
            f"{result['auto_fixable']} can be repaired automatically.",
            "success",
        )
        return redirect(url_for("admin_seo"))

    @app.post("/admin/seo/fix")
    @admin_required
    def admin_seo_fix():
        fixed = apply_seo_fixes()
        flash(f"Applied {fixed} safe SEO repairs.", "success")
        return redirect(url_for("admin_seo"))

    @app.post("/admin/automation/run")
    @admin_required
    def admin_automation_run():
        result = run_automation_cycle(force=True)
        flash(f"Automation cycle finished with status: {result['status']}.", "success" if result["status"] == "completed" else "warning")
        return redirect(url_for("admin_dashboard"))



def start_scheduler(app: Flask) -> None:
    if not app.config.get("SCHEDULER_ENABLED") or app.config.get("TESTING"):
        return
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    def scheduled_cycle():
        with app.app_context():
            run_automation_cycle(force=False)

    scheduler.add_job(
        scheduled_cycle,
        CronTrigger(hour=app.config["AUTOMATION_HOUR_UTC"], minute=0, timezone="UTC"),
        id="daily-content-cycle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    app.extensions["content_scheduler"] = scheduler

    # Render free services can restart or sleep through the scheduled hour. Run one
    # duplicate-protected cycle at startup so the blog still publishes once per day.
    threading.Thread(
        target=scheduled_cycle,
        name="startup-content-cycle",
        daemon=True,
    ).start()


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        print("Database initialized.")

    @app.cli.command("import-products")
    def import_products_command():
        result = import_products()
        print(result)

    @app.cli.command("generate-post")
    def generate_post_command():
        post = generate_post()
        print(f"Generated post {post.id}: {post.title} ({post.status})")

    @app.cli.command("seo-audit")
    def seo_audit_command():
        print(run_seo_audit())

    @app.cli.command("automation-run")
    def automation_run_command():
        print(run_automation_cycle(force=True))


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?")
    args, _ = parser.parse_known_args()
    if args.command in {"init-db", "import-products", "generate-post", "seo-audit", "automation-run"}:
        with app.app_context():
            if args.command == "init-db":
                db.create_all()
                print("Database initialized.")
            elif args.command == "import-products":
                print(import_products())
            elif args.command == "generate-post":
                print(generate_post())
            elif args.command == "seo-audit":
                print(run_seo_audit())
            elif args.command == "automation-run":
                print(run_automation_cycle(force=True))
    else:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
