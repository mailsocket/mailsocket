"Public API."

import re
from pathlib import Path

import rcssmin
from django.conf import settings

STATIC = Path(settings.BASE_DIR) / "apps" / "dashboard" / "static" / "dashboard"
SOURCE = STATIC / "app.css"
MINIFIED = STATIC / "app.min.css"


def test_app_min_css_is_rcssmin_of_app_css():
    expected = rcssmin.cssmin(SOURCE.read_text(encoding="utf-8")) + "\n"
    assert MINIFIED.read_text(encoding="utf-8") == expected, (
        "dashboard/app.min.css is stale — regenerate it from app.css (see operations notes, TD-15)"
    )


def test_templates_link_only_the_minified_stylesheet():
    roots = [Path(settings.BASE_DIR) / "apps", Path(settings.BASE_DIR) / "templates"]
    linked_source, linked_min = [], []
    for root in roots:
        for tpl in root.rglob("*.html"):
            text = tpl.read_text(encoding="utf-8")
            if re.search(r"static ['\"]dashboard/app\.css['\"]", text):
                linked_source.append(str(tpl))
            if re.search(r"static ['\"]dashboard/app\.min\.css['\"]", text):
                linked_min.append(str(tpl))
    assert linked_source == [], f"templates must link dashboard/app.min.css, not app.css: {linked_source}"
    assert len(linked_min) >= 12  # base, docs_base, landing, legal, try, 403/404/403_csrf, 4 accounts pages


def test_minified_css_keeps_every_selector_and_rule_text():
    """Cheap semantic check on top of byte equality: minification only drops
    comments, whitespace and the optional last ``;`` before ``}``, so after
    removing those both files have identical characters."""

    def squash(css):
        css = re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        return css.replace(";}", "}")

    assert squash(MINIFIED.read_text(encoding="utf-8")) == squash(SOURCE.read_text(encoding="utf-8"))
