# eBayMarketplace.biz Product Blog

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
