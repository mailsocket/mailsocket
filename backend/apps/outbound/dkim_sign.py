"""dkimpy-based signing at drain time (design §2 / §4).

NOT opendkim (deploy/milter/* — static tables baked into the image; this
feature never touches that path, and the milter is a no-op for any sender
other than noreply@mailsocket.app — confirmed at design time via
deploy/milter/signing-table). Sign IN THE APP, keyed from the DB
(Domain.dkim_private_key_enc, decrypted per-message), atomic with the send.
"""
import dkim

from .dkim_keys import decrypt_private_key


class DkimSigningError(Exception):
    pass


def sign_message(raw_message: bytes, *, domain_name: str, selector: str, private_key_enc: bytes) -> bytes:
    """Sign ``raw_message`` (a fully-formed RFC 5322 message, headers+body,
    CRLF line endings) and return the DKIM-Signature header LINE (with
    trailing CRLF), ready to prepend to the message.

    relaxed/relaxed canonicalization, signing exactly the header set the
    design calls out: From, To, Cc, Subject, Date, Message-ID, MIME-Version,
    Content-Type. d= is always ``domain_name`` (the From domain — see the
    alignment proof in the design §2 "Alignment math" note), never the
    envelope/bounce domain.
    """
    try:
        private_key_pem = decrypt_private_key(private_key_enc)
    except Exception as exc:  # pragma: no cover - defensive, fail closed
        raise DkimSigningError("dkim_key_decrypt_failed") from exc

    include_headers = [
        b"From",
        b"To",
        b"Cc",
        b"Subject",
        b"Date",
        b"Message-ID",
        b"MIME-Version",
        b"Content-Type",
        
        # these are ACTUALLY PRESENT in raw_message and silently skips an
        # absent header, so listing them unconditionally signs threading
        
        # these headers) byte-for-byte unaffected.
        b"In-Reply-To",
        b"References",
    ]
    try:
        signature = dkim.sign(
            message=raw_message,
            selector=selector.encode("ascii"),
            domain=domain_name.encode("ascii"),
            privkey=private_key_pem,
            canonicalize=(b"relaxed", b"relaxed"),
            include_headers=include_headers,
        )
    except dkim.DKIMException as exc:
        raise DkimSigningError(f"dkim_sign_failed:{exc}") from exc
    return signature


def verify_signed_message(signed_message: bytes) -> bool:
    """Test/verification helper only (design §7 DKIM test case): verify a
    fully-signed message (DKIM-Signature header + body) against whatever
    public key the header's d=/s= names — callers inject a DNS TXT lookup
    via dkim.DKIM(..., dnsfunc=...) when they need to avoid the real
    network; production code never calls this."""
    return dkim.verify(signed_message)
