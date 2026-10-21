"Public API."
import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.core.mail import get_connection, send_mail

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {
    "down": "\U0001F534",  # red circle
    "warn": "\U0001F7E0",  # orange circle
    "ok": "\U0001F7E2",  # green circle
    "money": "\U0001F4B0",  # money bag
    "info": "\u2139\ufe0f",  # information source
}
LEVELS = frozenset(LEVEL_EMOJI)

SMTP_TIMEOUT_SECONDS = 10
TELEGRAM_TIMEOUT_SECONDS = 5
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
# Same-key alerts inside this window are sent once (scope: the configured
# Django cache). Cross-process "exactly once" guarantees live in the callers'
# DB claims (wait_alerts, status transitions); this is a cheap extra guard.
DEDUPE_TTL_SECONDS = 300

OK = "ok"
DISABLED = "disabled"
DEDUPED = "deduped"

_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _clean(value):
    """One-line, header-safe string (subjects must not contain newlines)."""
    return " ".join(str(value if value is not None else "").split())


def _error(exc):
    label = f"error:{type(exc).__name__}"
    if isinstance(exc, urllib.error.HTTPError):
        label += f"({exc.code})"
    return label


def email_enabled():
    return bool(getattr(settings, "OPS_ALERT_EMAIL", ""))


def telegram_enabled():
    return bool(
        getattr(settings, "OPS_TELEGRAM_BOT_TOKEN", "")
        and getattr(settings, "OPS_TELEGRAM_CHAT_ID", "")
    )


def any_channel_enabled():
    return email_enabled() or telegram_enabled()


def delivered(result):
    """True when at least one channel accepted the alert."""
    return OK in (result or {}).values()


def failed(result):
    """True when nothing was delivered and at least one channel errored —
    callers with a DB claim release it so the next run retries."""
    values = (result or {}).values()
    return OK not in values and any(str(v).startswith("error") for v in values)


def _truncate_utf16(text, limit=TELEGRAM_MAX_CHARS):
    """Telegram counts UTF-16 code units; emoji outside the BMP count as 2."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    suffix = "\u2026"
    out, units = [], 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > limit - 1:
            break
        out.append(ch)
        units += width
    return "".join(out) + suffix


def _redact(text):
    text = _EMAIL_RE.sub("[email]", text)
    token = getattr(settings, "OPS_TELEGRAM_BOT_TOKEN", "")
    if token:
        text = text.replace(token, "[token]")
    return text


def telegram_text(level, title, body, link=None):
    parts = [f"{LEVEL_EMOJI.get(level, LEVEL_EMOJI['info'])} {_clean(title)}"]
    if body:
        parts.append(str(body).strip())
    if link:
        parts.append(str(link))
    return _truncate_utf16(_redact("\n\n".join(parts)))


def _send_email(title, body):
    recipient = getattr(settings, "OPS_ALERT_EMAIL", "")
    if not recipient:
        return DISABLED
    send_mail(
        subject=_clean(f"[mailsocket] {title}"),
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
        connection=get_connection(fail_silently=False, timeout=SMTP_TIMEOUT_SECONDS),
    )
    return OK


def _send_telegram(text):
    if not telegram_enabled():
        return DISABLED
    token = settings.OPS_TELEGRAM_BOT_TOKEN
    payload = json.dumps(
        {
            "chat_id": settings.OPS_TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "mailsocket-ops/1"},
    )
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
        raw = response.read(65536)
    try:
        ok = bool(json.loads(raw.decode("utf-8")).get("ok"))
    except (ValueError, AttributeError):
        ok = False
    return OK if ok else "error:TelegramNotOk"


def _run(channel, fn, *args):
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 — a channel must never raise
        result = _error(exc)
    if result.startswith("error"):
        # Type/status only: never str(exc) — it can embed the bot URL/token.
        logger.warning("ops_notify_failed channel=%s result=%s", channel, result)
    return result


def notify(level, title, body, *, link=None, dedupe_key=None, email_title=None, email_body=None):
    """Send one ops alert to email and Telegram. Never raises.

    ``title``/``body``/``link`` must be Telegram-safe (no customer emails,
    keys, tokens or message contents). ``email_title``/``email_body`` override
    what the email gets (default: title, and body plus the link)."""
    result = {"email": DISABLED, "telegram": DISABLED}
    try:
        if level not in LEVELS:
            level = "info"
        if dedupe_key:
            try:
                if not cache.add(f"ops-notify:{dedupe_key}", 1, DEDUPE_TTL_SECONDS):
                    return {"email": DEDUPED, "telegram": DEDUPED}
            except Exception:  # noqa: BLE001 — a broken cache must not block alerts
                logger.warning("ops_notify_dedupe_unavailable")
        if email_body is None:
            email_body = str(body or "")
            if link:
                email_body = f"{email_body}\n\n{link}"
        result["email"] = _run("email", _send_email, email_title or title, email_body)
        try:
            text = telegram_text(level, title, body, link)
        except Exception as exc:  # noqa: BLE001
            result["telegram"] = _error(exc)
            logger.warning("ops_notify_failed channel=telegram result=%s", result["telegram"])
        else:
            result["telegram"] = _run("telegram", _send_telegram, text)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.warning("ops_notify_failed channel=notify result=%s", _error(exc))
    return result
