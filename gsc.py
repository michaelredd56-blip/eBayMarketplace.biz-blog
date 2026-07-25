from __future__ import annotations

from datetime import date, timedelta

from flask import current_app, url_for
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from services import load_oauth_token, save_oauth_token, setting_get

SCOPES = ["https://www.googleapis.com/auth/webmasters"]


def configured() -> bool:
    return bool(current_app.config.get("GOOGLE_CLIENT_ID") and current_app.config.get("GOOGLE_CLIENT_SECRET"))


def client_config() -> dict:
    redirect = current_app.config.get("GOOGLE_REDIRECT_URI") or url_for("admin_gsc_callback", _external=True)
    return {
        "web": {
            "client_id": current_app.config["GOOGLE_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect],
        }
    }


def make_flow(state: str | None = None) -> Flow:
    redirect = current_app.config.get("GOOGLE_REDIRECT_URI") or url_for("admin_gsc_callback", _external=True)
    return Flow.from_client_config(client_config(), scopes=SCOPES, state=state, redirect_uri=redirect)


def credentials() -> Credentials | None:
    payload = load_oauth_token("google_search_console")
    if not payload:
        return None
    creds = Credentials.from_authorized_user_info(payload, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_oauth_token("google_search_console", {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        })
    return creds


def service():
    creds = credentials()
    if not creds:
        raise RuntimeError("Google Search Console is not connected.")
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def list_properties() -> list[dict]:
    response = service().sites().list().execute()
    return response.get("siteEntry", [])


def selected_property() -> str:
    return (
        setting_get("gsc_property")
        or current_app.config.get("GOOGLE_SEARCH_CONSOLE_PROPERTY", "")
        or current_app.config["SITE_URL"] + "/"
    )


def performance(days: int = 28, row_limit: int = 25) -> dict:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=max(days - 1, 1))
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query"],
        "rowLimit": row_limit,
        "dataState": "final",
    }
    response = service().searchanalytics().query(siteUrl=selected_property(), body=body).execute()
    rows = response.get("rows", [])
    totals = {
        "clicks": sum(float(row.get("clicks", 0)) for row in rows),
        "impressions": sum(float(row.get("impressions", 0)) for row in rows),
    }
    totals["ctr"] = totals["clicks"] / totals["impressions"] if totals["impressions"] else 0
    weighted_position = sum(float(row.get("position", 0)) * float(row.get("impressions", 0)) for row in rows)
    totals["position"] = weighted_position / totals["impressions"] if totals["impressions"] else 0
    return {"start": start.isoformat(), "end": end.isoformat(), "rows": rows, "totals": totals}


def list_sitemaps() -> list[dict]:
    response = service().sitemaps().list(siteUrl=selected_property()).execute()
    return response.get("sitemap", [])


def submit_sitemap(sitemap_url: str) -> None:
    service().sitemaps().submit(siteUrl=selected_property(), feedpath=sitemap_url).execute()


def inspect_url(url: str) -> dict:
    body = {"inspectionUrl": url, "siteUrl": selected_property(), "languageCode": "en-US"}
    return service().urlInspection().index().inspect(body=body).execute()


def performance_by_page(days: int = 28, row_limit: int = 100) -> list[dict]:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=max(days - 1, 1))
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["page"],
        "rowLimit": row_limit,
        "dataState": "final",
    }
    return service().searchanalytics().query(siteUrl=selected_property(), body=body).execute().get("rows", [])
