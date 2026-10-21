"""Linearity of mask_non_otp_spans on adversarial 1 MB inputs (ReDoS guard).

Moved out of test_parser.py (health ratchet: no >800-line file may grow).
Checked by the cost ratio of a 4x larger input (linear ~4x, quadratic ~16x),
not by a fixed time budget that flaked on a slow CI runner.
"""

import time

import pytest

from apps.ingest.parser import mask_non_otp_spans


def _best_of_3(body):
    best = float("inf")
    masked = None
    for _ in range(3):
        started = time.perf_counter()
        masked = mask_non_otp_spans(body)
        best = min(best, time.perf_counter() - started)
    return best, masked


def _assert_linear(make_body, label):
    """Linearity, not machine speed: a 4x larger input must cost ~4x (a
    quadratic scan costs ~16x). The absolute ceiling only catches a hang. A
    fixed 0.5 s budget flaked on a slow CI runner (0.636 s, 2026-10-13)."""
    small_t, _ = _best_of_3(make_body(250_000))
    big_t, masked = _best_of_3(make_body(1_000_000))
    ratio = big_t / max(small_t, 1e-4)
    assert ratio < 8, f"{label}: 4x input took {ratio:.1f}x ({small_t:.3f}s -> {big_t:.3f}s)"
    assert big_t < 5, f"{label}: 1 MB took {big_t:.3f}s"
    return masked


@pytest.mark.parametrize(
    "unit",
    [
        "https://a.io/((((",
        "https://a.io/))))",
        "(https://a.io/))))",
        "((((https://a.io/a)",
        "(https://a.io/(((()",
        "(x.io/a(()))",
        "(((((((((( https://a.io/1)",
        "( https://a.io/1(2)3)",
    ],
)
def test_r3_one_megabyte_unbalanced_paren_urls_are_linear(unit):
    def make(n):
        return (unit * (n // len(unit) + 1))[:n]

    masked = _assert_linear(make, f"mask of {unit!r}")
    assert len(masked) == 1_000_000


def test_r3_single_megabyte_url_of_open_parens_is_linear():
    masked = _assert_linear(lambda n: "https://a.io/" + "(" * n + "123456", "single paren URL")
    assert masked.endswith("______")


R2_UNITS = [
    "Visit https://e.com/a/1234?x=5678&y=(9012) www.q.io/77 例子.公司/8899 a1@b.io ",
    "a1.",
    "1.",
    "例子.",
    "https://x.io/(",
    "https://x.io/(a",
    "a-",
    "a@",
    "www.",
]


@pytest.mark.parametrize("unit", R2_UNITS)
def test_r2_one_megabyte_mask_with_many_urls_is_linear(unit):
    def make(n):
        return (unit * (n // len(unit) + 1))[:n]

    masked = _assert_linear(make, f"r2 mask of {unit!r}")
    assert len(masked) == 1_000_000


R4_UNITS = [
    ") (https://a.io/x)1 ",
    ")( https://a.io/1 ",
    "Prior note). (https://x.io/a?b=1)123456 ",
    ")" * 300 + "(" * 300 + " https://a.io/1 ",
    "(" * 300 + ")" * 600 + " https://a.io/1 ",
]


@pytest.mark.parametrize("unit", R4_UNITS)
def test_r4_one_megabyte_order_aware_depth_is_linear(unit):
    def make(n):
        return (unit * (n // len(unit) + 1))[:n]

    masked = _assert_linear(make, f"r4 mask of {unit[:40]!r}")
    assert len(masked) == 1_000_000


def test_r4_one_megabyte_single_prose_gap_is_linear():
    masked = _assert_linear(lambda n: (")(" * (n // 2)) + " https://a.io/q)482193", "r4 single prose gap")
    assert masked.endswith("https://a.io/q)482193")
