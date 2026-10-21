"Public API."

from unittest import mock

import pytest

from apps.outbound.transport import SmtpTransportError, send_via_postfix
from apps.outbound.validation import AddressFormatError, validate_recipient_address


@pytest.mark.parametrize(
    "value", ["café@example.com", "a@café.com", "аdmin@example.com", "x@exa\u200bmple.com"]
)
def test_gate_rejects_non_ascii_recipient(value):
    with pytest.raises(AddressFormatError):
        validate_recipient_address(value)


def test_gate_still_accepts_plain_ascii():
    assert validate_recipient_address("bob@example.com") == "bob@example.com"


def test_transport_maps_unicode_encode_error_to_permanent_failure(settings):
    settings.EMAIL_HOST, settings.EMAIL_PORT = "127.0.0.1", 2525

    class _Smtp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendmail(self, _frm, to, _raw):
            return "x".join(to).encode("ascii")  # what quoteaddr(...).encode does

    with mock.patch("apps.outbound.transport.smtplib.SMTP", _Smtp):
        with pytest.raises(SmtpTransportError) as exc:
            send_via_postfix(envelope_from="b@ok.test", envelope_to=["café@example.com"], raw_message=b"x")
    assert exc.value.classification == "permanent"
    assert exc.value.detail == "invalid_recipient_encoding"
