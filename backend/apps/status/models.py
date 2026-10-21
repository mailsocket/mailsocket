from django.db import models


class ComponentCheck(models.Model):
    """One probe result for one public component, written every 5 minutes by
    the ``status_probe`` management command (host cron, see operations notes). Rows
    older than ``RETENTION`` are deleted by the hourly retention job."""

    class Component(models.TextChoices):
        WEB = "web", "Website & dashboard"
        API = "api", "REST API"
        INBOUND_MAIL = "inbound_mail", "Inbound mail (SMTP)"
        WAIT = "wait", "Wait endpoint (long-poll)"
        MCP = "mcp", "MCP server"

    id = models.BigAutoField(primary_key=True)
    component = models.CharField(max_length=32, choices=Component.choices)
    ok = models.BooleanField()
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    checked_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["component", "-checked_at"], name="status_check_comp_time"),
        ]

    def __str__(self):
        return f"{self.component} {'ok' if self.ok else 'fail'} @ {self.checked_at:%Y-%m-%d %H:%M}"


class Incident(models.Model):
    """A human-written incident note, edited in Django admin and shown on
    /status. Nothing creates these automatically."""

    class Status(models.TextChoices):
        INVESTIGATING = "investigating", "Investigating"
        IDENTIFIED = "identified", "Identified"
        MONITORING = "monitoring", "Monitoring"
        RESOLVED = "resolved", "Resolved"

    id = models.BigAutoField(primary_key=True)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INVESTIGATING)
    body = models.TextField(blank=True)
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return self.title


class ComponentAlertState(models.Model):
    """Last ALERTED state per component (tg-notify). ``status_probe`` flips it
    with a conditional UPDATE (``WHERE state = <previous>``) so only one of
    any number of concurrent cron runs wins a transition and sends the alert.
    Only two states are tracked: a single failed check (Degraded) never
    alerts, to avoid flapping."""

    OPERATIONAL = "operational"
    DOWN = "down"
    STATES = [(OPERATIONAL, "Operational"), (DOWN, "Down")]

    component = models.CharField(max_length=32, choices=ComponentCheck.Component.choices, unique=True)
    state = models.CharField(max_length=16, choices=STATES, default=OPERATIONAL)
    # When the current DOWN streak started (first failed check); NULL while up.
    down_since = models.DateTimeField(null=True, blank=True)
    changed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.component}: {self.state}"
