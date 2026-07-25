from __future__ import annotations

import os
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(os.getenv("DATABASE_URL", "sqlite:///blog.db"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    SITE_NAME = os.getenv("SITE_NAME", "Marketplace Finds")
    SITE_TAGLINE = os.getenv("SITE_TAGLINE", "Useful product guides and smart shopping ideas")
    SITE_URL = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")
    SOURCE_SITE_URL = os.getenv("SOURCE_SITE_URL", "https://ebaymarketplace.biz").rstrip("/")
    SOURCE_SITEMAP_URL = os.getenv("SOURCE_SITEMAP_URL", "").strip()
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
    GOOGLE_SEARCH_CONSOLE_PROPERTY = os.getenv("GOOGLE_SEARCH_CONSOLE_PROPERTY", "")
    TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")

    AUTO_PUBLISH = _bool("AUTO_PUBLISH", False)
    SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", False)
    POSTS_PER_RUN = max(1, min(_int("POSTS_PER_RUN", 1), 10))
    AUTOMATION_HOUR_UTC = min(max(_int("AUTOMATION_HOUR_UTC", 13), 0), 23)
    PRODUCT_IMPORT_LIMIT = max(1, min(_int("PRODUCT_IMPORT_LIMIT", 25), 250))

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", urlparse(SITE_URL).scheme == "https")
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    SCHEDULER_ENABLED = False
    ADMIN_PASSWORD = "test-password"
