"Public API."

import re
from pathlib import Path

import pytest
import rjsmin
from django.conf import settings

STATIC = Path(settings.BASE_DIR) / "apps" / "dashboard" / "static" / "dashboard"
SOURCES = ("app", "playground", "resend-countdown", "try")


@pytest.mark.parametrize("name", SOURCES)
def test_min_js_is_rjsmin_of_source(name):
    expected = rjsmin.jsmin((STATIC / f"{name}.js").read_text(encoding="utf-8")) + "\n"
    assert (STATIC / f"{name}.min.js").read_text(encoding="utf-8") == expected, (
        f"dashboard/{name}.min.js is stale - regenerate it (operations notes, TD-15)"
    )


def test_templates_link_only_minified_scripts():
    roots = [Path(settings.BASE_DIR) / "apps", Path(settings.BASE_DIR) / "templates"]
    source_links, min_links = [], []
    for root in roots:
        for tpl in root.rglob("*.html"):
            text = tpl.read_text(encoding="utf-8")
            pattern = r"static ['\"]dashboard/({})\.js['\"]".format("|".join(map(re.escape, SOURCES)))
            if re.search(pattern, text):
                source_links.append(str(tpl))
            if re.search(r"static ['\"]dashboard/[a-z-]+\.min\.js['\"]", text):
                min_links.append(str(tpl))
    assert source_links == [], f"templates must link the .min.js copies: {source_links}"
    assert len(min_links) >= 6  # base, docs_base, landing, legal, try, check_email
