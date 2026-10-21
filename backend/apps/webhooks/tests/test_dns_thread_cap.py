"Public API."

import threading

import pytest
from django.test import override_settings


@override_settings(WEBHOOK_DNS_MAX_THREADS=2)
def test_dns_threads_are_capped_and_fail_closed_when_exhausted():
    from apps.webhooks import security

    release = threading.Event()
    started = []

    def hanging(hostname):
        started.append(hostname)
        release.wait(5)
        return ["93.184.216.34"]

    try:
        for _ in range(2):
            with pytest.raises(security.SSRFValidationError) as exc:
                security._resolve_bounded("h.example", hanging, 0.05)
            assert exc.value.code == "ssrf_dns_timeout"
        assert len(started) == 2
        # pool full: refused immediately, no third thread is started
        with pytest.raises(security.SSRFValidationError) as exc:
            security._resolve_bounded("h.example", hanging, 0.05)
        assert exc.value.code == "ssrf_dns_timeout"
        assert len(started) == 2
    finally:
        release.set()
    # once the stuck lookups return, slots are free again
    for _ in range(50):
        if security._dns_slots_in_use() == 0:
            break
        threading.Event().wait(0.02)
    assert security._dns_slots_in_use() == 0
    assert security._resolve_bounded("h.example", lambda h: ["93.184.216.34"], 1.0) == ["93.184.216.34"]
