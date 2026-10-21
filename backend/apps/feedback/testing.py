"""Test helper for byte-identical ("no oracle") comparisons of error pages.

Since "Report a problem", 404/403/500 pages show the request's own
X-Request-Id (random per request) and a report link carrying it plus the
requested path. Both come from the REQUEST, not from server-side state, so
they are noise for an existence-oracle comparison, exactly like the
per-session CSRF token the team tests already strip.
"""
import re

_REFERENCE_RE = re.compile(rb'<p class="error-ref">Reference: <code>[^<]*</code></p>')
_REPORT_LINK_RE = re.compile(rb'href="/feedback/\?[^"]*"')


def strip_request_noise(content: bytes) -> bytes:
    content = _REFERENCE_RE.sub(b'<p class="error-ref">Reference: <code>STRIPPED</code></p>', content)
    return _REPORT_LINK_RE.sub(b'href="/feedback/?STRIPPED"', content)
