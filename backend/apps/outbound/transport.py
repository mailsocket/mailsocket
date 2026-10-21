"""SMTP submission to the Postfix trust path (design §4: "smtplib to the
Postfix container, the 172.30.0.10 trust path, main.cf:10"). Deliberately
minimal — a thin wrapper so the drain engine can inject a fake transport in
tests (never dials the real network in a test).
"""

import smtplib
from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class SmtpSendResult:
    queue_id: str
    classification: str  # "sent" | "tempfail" | "permanent"
    detail: str = ""


class SmtpTransportError(Exception):
    def __init__(self, classification: str, detail: str = ""):
        self.classification = classification
        self.detail = detail
        super().__init__(detail or classification)


def send_via_postfix(*, envelope_from: str, envelope_to: list[str], raw_message: bytes) -> SmtpSendResult:
    """Submit ``raw_message`` (already DKIM-signed) to the Postfix
    container over the existing EMAIL_HOST/EMAIL_PORT trust path (the same
    settings apps.accounts.views' _send_auth_mail relies on transitively via
    django.core.mail — here we talk smtplib directly since we construct the
    envelope ourselves, VERP MAIL FROM included).
    """
    host = settings.EMAIL_HOST
    port = settings.EMAIL_PORT
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            refused = smtp.sendmail(envelope_from, envelope_to, raw_message)
    except smtplib.SMTPResponseException as exc:
        if 400 <= exc.smtp_code < 500:
            raise SmtpTransportError("tempfail", str(exc.smtp_error)) from exc
        raise SmtpTransportError("permanent", str(exc.smtp_error)) from exc
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as exc:
        raise SmtpTransportError("tempfail", str(exc)) from exc
    except smtplib.SMTPException as exc:
        raise SmtpTransportError("permanent", str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise SmtpTransportError("tempfail", str(exc)) from exc
    except UnicodeEncodeError as exc:
        
        # row permanently instead of aborting the global drain pass.
        raise SmtpTransportError("permanent", "invalid_recipient_encoding") from exc
    if refused:
        # Partial refusal (some recipients rejected, others accepted) — V1
        # treats ANY refusal as a permanent failure for the whole send
        # rather than silently under-delivering; a customer resubmits with
        # the corrected recipient list.
        raise SmtpTransportError("permanent", f"recipients refused: {refused}")
    return SmtpSendResult(queue_id="", classification="sent")
