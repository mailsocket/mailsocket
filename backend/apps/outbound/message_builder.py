"Public API."
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from django.conf import settings


def build_message_id(outbound_email) -> str:
    return f"<{outbound_email.public_id}.{uuid.uuid4().hex}@{settings.MAIL_DOMAIN}>"


def build_raw_message(outbound_email) -> tuple[bytes, str]:
    """Returns (raw_bytes_with_crlf, message_id). raw_bytes has NO
    DKIM-Signature header yet — the drain engine prepends the signature
    dkimpy computes for exactly these bytes."""
    msg = EmailMessage()
    msg["From"] = outbound_email.from_address
    msg["To"] = ", ".join(outbound_email.to)
    if outbound_email.cc:
        msg["Cc"] = ", ".join(outbound_email.cc)
    if outbound_email.reply_to:
        msg["Reply-To"] = outbound_email.reply_to
    msg["Subject"] = outbound_email.subject
    msg["Date"] = formatdate(localtime=False)
    message_id = build_message_id(outbound_email)
    msg["Message-ID"] = message_id
    
    
    # so both are already CRLF-terminated in the returned bytes exactly like
    # every other header here.
    if outbound_email.in_reply_to:
        msg["In-Reply-To"] = outbound_email.in_reply_to
    if outbound_email.references:
        msg["References"] = outbound_email.references

    text = outbound_email.body_text or ""
    html = outbound_email.body_html or ""
    if html:
        msg.set_content(text or " ")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)

    # email.message.EmailMessage generates \n line endings by default via
    # BytesGenerator with policy.default (which uses \n) — dkimpy and SMTP
    # both want \r\n. Normalize once, here, at the single construction site.
    raw = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
    return raw, message_id
