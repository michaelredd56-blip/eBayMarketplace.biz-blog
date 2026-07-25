from __future__ import annotations

import argparse
import hmac
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

import gsc
from config import Config
from models import AutomationRun, OAuthToken, Post, Product, SEOIssue, db, utcnow
from services import (
    add_manual_product,
    apply_seo_fixes,
    delete_oauth_token,
    generate_post,
    import_products,
    publish_post,
    run_automation_cycle,
    run_seo_audit,
    save_oauth_token,
    sync_search_console_signals,
    record_url_inspection,
    setting_set,
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
        canonical = f"{app.config['SITE_URL']}/blog/{post.slug}"
        return render_template("article.html", post=post, related=related, canonical=canonical)

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
        body = f"User-agent: *\nAllow: /\nDisallow: /admin/\nSitemap: {app.config['SITE_URL']}/sitemap.xml\n"
        return Response(body, mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        static_urls = [
            (app.config["SITE_URL"] + "/", utcnow()),
            (app.config["SITE_URL"] + "/about", utcnow()),
            (app.config["SITE_URL"] + "/affiliate-disclosure", utcnow()),
        ]
        posts = Post.query.filter_by(status="published").order_by(desc(Post.updated_at)).all()
        return Response(render_template("sitemap.xml", static_urls=static_urls, posts=posts), mimetype="application/xml")

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
        gsc_result = sync_search_console_signals()
        flash(f"Audit completed. {result['open_issues']} open issues; {result['auto_fixable']} can be repaired automatically. Search Console sync: {gsc_result['status']}.", "success")
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

    @app.get("/admin/gsc")
    @admin_required
    def admin_gsc():
        connected = OAuthToken.query.filter_by(provider="google_search_console").first() is not None
        properties = []
        metrics = None
        sitemaps = []
        error = None
        if connected:
            try:
                properties = gsc.list_properties()
                metrics = gsc.performance()
                sitemaps = gsc.list_sitemaps()
            except Exception as exc:
                app.logger.exception("Search Console data request failed")
                error = str(exc)
        return render_template(
            "admin_gsc.html",
            connected=connected,
            configured=gsc.configured(),
            properties=properties,
            metrics=metrics,
            sitemaps=sitemaps,
            selected_property=gsc.selected_property(),
            error=error,
        )

    @app.get("/admin/gsc/connect")
    @admin_required
    def admin_gsc_connect():
        if not gsc.configured():
            flash("Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render before connecting.", "danger")
            return redirect(url_for("admin_gsc"))
        flow = gsc.make_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        session["google_oauth_state"] = state
        return redirect(authorization_url)

    @app.get("/admin/gsc/callback")
    @admin_required
    def admin_gsc_callback():
        state = session.pop("google_oauth_state", None)
        if not state or state != request.args.get("state"):
            abort(400, description="Invalid OAuth state.")
        flow = gsc.make_flow(state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        save_oauth_token("google_search_console", {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or gsc.SCOPES),
        })
        flash("Google Search Console connected successfully.", "success")
        return redirect(url_for("admin_gsc"))

    @app.post("/admin/gsc/disconnect")
    @admin_required
    def admin_gsc_disconnect():
        delete_oauth_token("google_search_console")
        flash("Google Search Console disconnected.", "success")
        return redirect(url_for("admin_gsc"))

    @app.post("/admin/gsc/property")
    @admin_required
    def admin_gsc_property():
        property_url = request.form.get("property_url", "").strip()
        if not property_url:
            flash("Select a Search Console property.", "danger")
        else:
            setting_set("gsc_property", property_url)
            flash("Search Console property saved.", "success")
        return redirect(url_for("admin_gsc"))

    @app.post("/admin/gsc/sitemap")
    @admin_required
    def admin_gsc_sitemap():
        sitemap_url = f"{app.config['SITE_URL']}/sitemap.xml"
        try:
            gsc.submit_sitemap(sitemap_url)
            flash(f"Submitted {sitemap_url} to Search Console.", "success")
        except Exception as exc:
            app.logger.exception("Sitemap submission failed")
            flash(f"Sitemap submission failed: {exc}", "danger")
        return redirect(url_for("admin_gsc"))

    @app.post("/admin/gsc/inspect")
    @admin_required
    def admin_gsc_inspect():
        inspection_url = request.form.get("inspection_url", "").strip()
        if not inspection_url.startswith(app.config["SITE_URL"]):
            flash("The inspected URL must belong to this blog.", "danger")
            return redirect(url_for("admin_gsc"))
        try:
            result = gsc.inspect_url(inspection_url)
            summary = record_url_inspection(inspection_url, result)
            flash(f"URL inspection completed. Verdict: {summary['verdict']}; coverage: {summary['coverage']}.", "success" if summary["verdict"] == "PASS" else "warning")
        except Exception as exc:
            app.logger.exception("URL inspection failed")
            flash(f"URL inspection failed: {exc}", "danger")
        return redirect(url_for("admin_gsc"))


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
