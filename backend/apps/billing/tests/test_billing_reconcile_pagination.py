"Public API."

import urllib.parse

import pytest

from apps.billing.tests.test_billing_reconcile import (
    BUSINESS,
    HC_URL,
    Edge,
    _Resp,
    alerts,
    local,
    ls_sub,
    plans_snapshot,
    run_cmd,
)

pytestmark = pytest.mark.django_db


class ShiftingEdge(Edge):
    """Edge whose page N carries meta.page from ``metas[N]`` (None -> no meta at all)."""

    def __init__(self, pages, metas, **kw):
        super().__init__(pages, **kw)
        self.metas = metas

    def _ls(self, method, path):
        if not path.startswith("/subscriptions"):
            return super()._ls(method, path)
        number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page[number]"][0])
        meta = self.metas.get(number, {"currentPage": number, "lastPage": len(self.pages)})
        body = {"data": self.pages[number - 1]}
        if meta is not None:
            body["meta"] = {"page": meta}
        return _Resp(body)


def page_meta(current, last, total=None):
    meta = {"currentPage": current, "lastPage": last}
    return meta if total is None else {**meta, "total": total}


THREE = [[ls_sub(1301)], [ls_sub(1302)], [ls_sub(1303)]]
PAGINATION_ERRORS = {
    
    "shrinking_last_page": (
        THREE,
        {2: page_meta(2, 1)},
        "inconsistent pagination on page 2: currentPage/lastPage/total=2/1/None",
    ),
    "growing_last_page": (
        THREE + [[ls_sub(1304)]],
        {1: page_meta(1, 3), 2: page_meta(2, 4)},
        "inconsistent pagination on page 2: currentPage/lastPage/total=2/4/None, expected 2/3/None",
    ),
    "wrong_current_page": (
        THREE,
        {2: page_meta(1, 3)},
        "inconsistent pagination on page 2: currentPage/lastPage/total=1/3/None",
    ),
    "missing_current_page": (THREE, {3: {"lastPage": 3}}, "currentPage/lastPage/total=None/3/None"),
    "bool_current_page": (THREE, {1: {"currentPage": True, "lastPage": 3}}, "on page 1: currentPage"),
    "empty_middle_page": ([[ls_sub(1301)], [], [ls_sub(1303)]], {}, "empty page 2 before lastPage 3"),
    "empty_first_page": ([[], [ls_sub(1302)]], {}, "empty page 1 before lastPage 2"),
    "missing_meta_on_page_2": (THREE, {2: None}, "malformed subscriptions page"),
    "missing_last_page_on_page_2": (THREE, {2: {"currentPage": 2}}, "malformed subscriptions page"),
    "total_changes": (
        THREE,
        {1: page_meta(1, 3, 3), 2: page_meta(2, 3, 3), 3: page_meta(3, 3, 2)},
        "currentPage/lastPage/total=3/3/2, expected 3/3/3",
    ),
    "total_appears_later": (THREE, {3: page_meta(3, 3, 3)}, "total=3/3/3, expected 3/3/None"),
    "total_not_an_int": (THREE, {1: page_meta(1, 3, "3")}, "malformed subscriptions page"),
    "total_bool": (THREE, {1: page_meta(1, 3, True)}, "malformed subscriptions page"),
    "fewer_items_than_total": (
        THREE,
        {n: page_meta(n, 3, 4) for n in (1, 2, 3)},
        "LS listed 3 distinct subscriptions, page 1 said total=4",
    ),
    "duplicate_item_across_pages": (
        [[ls_sub(1301)], [ls_sub(1301)], [ls_sub(1303)]],
        {n: page_meta(n, 3, 3) for n in (1, 2, 3)},
        "LS listed 2 distinct subscriptions, page 1 said total=3",
    ),
}


@pytest.mark.parametrize("case", sorted(PAGINATION_ERRORS))
def test_bl21_inconsistent_pagination_is_exit_2_never_clean(case):
    # BL-21 r2: lastPage/total are pinned from page 1; a later page that disagrees, says the
    # wrong currentPage, is empty before lastPage or lacks meta -> exit 2, no clean, no alert, no ping.
    pages, metas, expected = PAGINATION_ERRORS[case]
    for sub in {item["id"] for page in pages for item in page}:
        local(f"rc_pgx_{sub}", sub)
    before = plans_snapshot()
    edge = ShiftingEdge(pages, metas)
    code, out, err = run_cmd(edge, HEALTHCHECKS_RECONCILE_URL=HC_URL)
    assert (code, out, edge.pinged(), alerts()) == (2, "", [], [])
    (line,) = err.splitlines()
    assert line.startswith("billing_reconcile ERROR: ") and line.endswith(" — nothing was compared, no ping")
    assert expected in line
    if expected == "malformed subscriptions page":  # exact, not a substring of a longer message
        assert line == f"billing_reconcile ERROR: {expected} — nothing was compared, no ping"
    assert plans_snapshot() == before


def test_bl21_shrinking_last_page_never_stops_early_even_when_pages_read_look_clean():
    
    for sub in ("1301", "1302", "1303"):
        local(f"rc_shr_{sub}", sub)
    pages = [[ls_sub(1301)], [ls_sub(1302)], [ls_sub(1303, variant=BUSINESS)]]
    edge = ShiftingEdge(pages, {2: page_meta(2, 1)})
    code, out, _ = run_cmd(edge)
    assert (code, "clean" in out) == (2, False)
    assert len([p for p in edge.ls_paths() if p.startswith("/subscriptions")]) == 2


def test_bl21_consistent_pagination_with_total_and_empty_last_page_is_clean():
    # total pinned and matched; a single empty page (no subscriptions) and an empty LAST page are fine.
    assert run_cmd(ShiftingEdge([[]], {1: page_meta(1, 1, 0)}))[0] == 0
    for sub in ("1401", "1402"):
        local(f"rc_ok_{sub}", sub)
    pages = [[ls_sub(1401)], [ls_sub(1402)], []]
    metas = {n: page_meta(n, 3, 2) for n in (1, 2, 3)}
    code, out, _ = run_cmd(ShiftingEdge(pages, metas))
    assert code == 0 and "billing_reconcile clean ls_subscriptions=2 mismatches=0" in out
