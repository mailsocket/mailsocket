"Public API."
from django.conf import settings
from django.db import models

MESSAGE_MAX_CHARS = 2000
PAGE_PATH_MAX_CHARS = 200
USER_AGENT_MAX_CHARS = 300


class FeedbackReport(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ANSWERED = "answered", "Answered"
        CLOSED = "closed", "Closed"

    # varchar(2000): the DB refuses anything the form somehow let through.
    message = models.CharField(max_length=MESSAGE_MAX_CHARS)
    # Optional when signed in (the account email is used), required anonymous.
    email = models.EmailField(max_length=254, blank=True)
    # Path only (no query string, no host), validated as a local path.
    page_path = models.CharField(max_length=PAGE_PATH_MAX_CHARS, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="feedback_reports",
    )
    # Plan key of the acting account at submit time ("" = anonymous).
    plan = models.CharField(max_length=20, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=USER_AGENT_MAX_CHARS, blank=True)
    # HMAC-SHA256 of the client IP (AUTH_RATE_LIMIT_SECRET); never the raw IP.
    ip_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Feedback #{self.pk}"


class FeedbackAlertBreaker(models.Model):
    "Public API."

    key = models.CharField(max_length=16, unique=True)
    window_started_at = models.DateTimeField(null=True, blank=True)
    sent = models.PositiveIntegerField(default=0)
    suppressed = models.PositiveIntegerField(default=0)
    last_digest_at = models.DateTimeField(null=True, blank=True)
