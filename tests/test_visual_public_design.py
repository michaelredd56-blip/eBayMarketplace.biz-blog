from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_base_loads_public_visual_theme_after_core_styles():
    template = _read("templates/base.html")
    core_position = template.index("style.css")
    visual_position = template.index("visual-upgrade.css")

    assert visual_position > core_position


def test_homepage_uses_product_images_as_visual_backdrop_and_cards():
    template = _read("templates/index.html")

    assert "--hero-image" in template
    assert "hero-copy-panel" in template
    assert "visual-product-collage" in template
    assert "visual-card-image" in template
    assert "visual-value-band" in template
    assert "Recently curated products" in template


def test_article_page_uses_product_backdrop_and_visual_related_cards():
    template = _read("templates/article.html")

    assert "--article-image" in template
    assert "article-title-panel" in template
    assert "article-image-frame" in template
    assert "visual-article-content" in template
    assert "item.product.image_url" in template
    assert "image-read-badge" in template


def test_visual_theme_is_responsive_and_scoped_to_public_components():
    stylesheet = _read("static/visual-upgrade.css")

    assert ".visual-hero.has-hero-image" in stylesheet
    assert ".visual-article-hero.has-article-image" in stylesheet
    assert ".visual-post-card" in stylesheet
    assert ".generated-product-image" in stylesheet
    assert "@media (max-width: 620px)" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet
