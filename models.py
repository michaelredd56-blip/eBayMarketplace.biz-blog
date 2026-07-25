from __future__ import annotations

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint


db = SQLAlchemy()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    slug = db.Column(db.String(550), nullable=False, unique=True, index=True)
    source_url = db.Column(db.Text, nullable=False, unique=True)
    affiliate_url = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.Text)
    price = db.Column(db.String(80))
    currency = db.Column(db.String(8), default="USD")
    category = db.Column(db.String(180), default="Featured")
    description = db.Column(db.Text)
    brand = db.Column(db.String(180))
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    imported_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    posts = db.relationship("Post", back_populates="product", lazy=True)


class Post(db.Model):
    __tablename__ = "posts"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)
    title = db.Column(db.String(500), nullable=False)
    slug = db.Column(db.String(550), nullable=False, unique=True, index=True)
    excerpt = db.Column(db.Text, nullable=False)
    content_html = db.Column(db.Text, nullable=False)
    meta_title = db.Column(db.String(255), nullable=False)
    meta_description = db.Column(db.String(320), nullable=False)
    focus_keyword = db.Column(db.String(255))
    category = db.Column(db.String(180), default="Shopping Guides")
    status = db.Column(db.String(30), default="draft", nullable=False, index=True)
    auto_generated = db.Column(db.Boolean, default=True, nullable=False)
    published_at = db.Column(db.DateTime(timezone=True), index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    product = db.relationship("Product", back_populates="posts")


class OAuthToken(db.Model):
    __tablename__ = "oauth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(50), nullable=False, unique=True)
    encrypted_payload = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SiteSetting(db.Model):
    __tablename__ = "site_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), nullable=False, unique=True, index=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SEOIssue(db.Model):
    __tablename__ = "seo_issues"

    id = db.Column(db.Integer, primary_key=True)
    issue_key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    issue_type = db.Column(db.String(120), nullable=False)
    severity = db.Column(db.String(20), default="warning", nullable=False)
    page_url = db.Column(db.Text)
    details = db.Column(db.Text, nullable=False)
    auto_fixable = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(30), default="open", nullable=False, index=True)
    detected_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at = db.Column(db.DateTime(timezone=True))


class AutomationRun(db.Model):
    __tablename__ = "automation_runs"
    __table_args__ = (UniqueConstraint("run_key", name="uq_automation_run_key"),)

    id = db.Column(db.Integer, primary_key=True)
    run_key = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(30), default="started", nullable=False)
    details = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True))
