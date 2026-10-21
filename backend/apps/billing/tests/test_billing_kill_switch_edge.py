"""Billing kill switch r3/r4: registry + HTTP-edge enforcement (split out of
test_billing_kill_switch.py to keep that file under the 800-line limit).

  * Registry: every production module under backend/ (not only apps/) that
    references _ls_post / _ls_patch / _ls_delete / _ls_send_json must be @refuse_when_paused
    or in kill_switch.EXEMPT; a writer name as a string literal (getattr /
    vars / __dict__ / dotted path) outside apps.billing.views fails unless
    allow-listed in WRITER_STRING_ALLOW.
    _ls_send_json (the POST/PATCH edge) may be called only by _ls_post and
    _ls_patch.
  * Edge (r4): the check lives in _ls_send_json, the lowest write-capable
    function, so with the switch off it and its wrappers _ls_post / _ls_patch
    raise BillingChangesPaused before any network I/O (direct, getattr and
    vars calls; only urllib.request.urlopen is patched); any method other
    than POST/PATCH is a ValueError. _ls_delete (account erasure
    cancellation) is exempt and still reaches LS.
"""

import ast
import re
from pathlib import Path

import pytest
from django.conf import settings as dj_settings
from django.test import override_settings

from apps.billing import kill_switch
from apps.billing import views as bviews
from apps.billing.tests.test_billing_kill_switch import OFF, ON, ls_edge

# --- 3b. Registry: no LS-writing path without a guard or an explicit exemption --

LS_WRITERS = {"_ls_post", "_ls_patch", "_ls_delete", "_ls_send_json"}
LS_EDGE = "_ls_send_json"  # the POST/PATCH money-write edge; wrapped only by:
LS_EDGE_CALLERS = {f"apps.billing.views.{n}" for n in ("_ls_post", "_ls_patch")}
BACKEND_DIR = Path(dj_settings.BASE_DIR)
WRITERS_HOME = "apps.billing.views"  # defines the writers; may name them freely
# module -> reason: the ONLY production modules allowed to hold an LS writer
# name as a string (getattr / vars / __dict__ / mock targets). Empty today.
WRITER_STRING_ALLOW: dict[str, str] = {}
_WRITER_STRING = re.compile(r"(^|\.)_ls_(post|patch|delete|send_json)?$")  # also the bare "_ls_" prefix
_NON_PRODUCTION_PARTS = {"tests", "migrations", ".venv", "venv", "node_modules", "__pycache__"}


def _production_modules(root):
    """Every non-test, non-migration .py under backend/ (apps, config, ...)."""
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _NON_PRODUCTION_PARTS & set(rel.parts) or rel.name.startswith("test_"):
            continue
        if rel.name != "conftest.py":
            yield path


def _module_name(path, root):
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _writer_name_strings(module, tree):
    """A writer name as a string literal is reflective access the name scan
    cannot follow (getattr(v, "_ls_patch"), vars(v)["_ls_post"], __dict__)."""
    if module == WRITERS_HOME or module in WRITER_STRING_ALLOW:
        return []
    return [
        f"{module}: LS write helper name as a string {n.value!r} (line {n.lineno})"
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _WRITER_STRING.search(n.value)
    ]


def _references(node, names):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
    return False


def _references_writer(node):
    return _references(node, LS_WRITERS)


def _is_guarded(fn):
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name == "refuse_when_paused":
            return True
    return False


_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _aliased_writer_imports(module, node):
    """An LS write helper imported under another name would hide from the scan."""
    return [
        f"{module}: aliased import of {a.name} as {a.asname}"
        for a in node.names
        if a.name in LS_WRITERS and a.asname not in (None, a.name)
    ]


def _split_top_level(module, node):
    """(qualified functions, other nodes) for one top-level statement:
    class bodies yield their methods, a function yields itself."""
    if isinstance(node, ast.ClassDef):
        fns = [(f"{module}.{node.name}.{f.name}", f) for f in node.body if isinstance(f, _FUNC_TYPES)]
        return fns, [n for n in node.body if not isinstance(n, _FUNC_TYPES)]
    if isinstance(node, _FUNC_TYPES):
        return [(f"{module}.{node.name}", node)], []
    return [], [node]


def _scan_module(path, root, writers, guarded, problems):
    module = _module_name(path, root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems.extend(_writer_name_strings(module, tree))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            problems.extend(_aliased_writer_imports(module, node))
            continue
        fns, rest = _split_top_level(module, node)
        for qual, fn in fns:
            if _is_guarded(fn):
                guarded.add(qual)
            if fn.name not in LS_WRITERS and _references_writer(fn):
                writers[qual] = _is_guarded(fn)
        problems.extend(
            f"{module}: LS write helper referenced outside a function (line {other.lineno})"
            for other in rest
            if _references_writer(other)
        )


def _scan_ls_writers(root=BACKEND_DIR):
    """(writers, guarded_defs, problems) over every production module under
    backend/. A writer is a top-level function (or class method) whose
    body references an LS write helper by name or attribute; nested
    functions count for their enclosing top-level function. A writer name
    as a string literal outside WRITERS_HOME is a problem (reflection)."""
    writers, guarded, problems = {}, set(), []
    for path in _production_modules(root):
        _scan_module(path, root, writers, guarded, problems)
    return writers, guarded, problems


def _edge_callers(root=BACKEND_DIR):
    """Qualified top-level functions/methods (or "<module>:<line>" for a
    module-level use) anywhere in production code that reference LS_EDGE."""
    callers = set()
    for path in _production_modules(root):
        module = _module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            fns, rest = _split_top_level(module, node)
            callers |= {q for q, fn in fns if fn.name != LS_EDGE and _references(fn, {LS_EDGE})}
            callers |= {f"{module}:{o.lineno}" for o in rest if _references(o, {LS_EDGE})}
    return callers


def test_registry_ls_send_json_is_called_only_by_ls_post_and_ls_patch():
    assert _edge_callers() == LS_EDGE_CALLERS


def test_registry_edge_caller_scan_sees_a_new_caller(tmp_path):
    (tmp_path / "config").mkdir()
    call = "views._ls_send_json('POST', '/c', {}, (3, 10))"
    (tmp_path / "config" / "x.py").write_text(
        f"from apps.billing import views\n\n\ndef sneaky():\n    {call}\n"
    )
    assert _edge_callers(tmp_path) == {"config.x.sneaky"}


def test_registry_every_ls_write_caller_is_guarded_or_explicitly_exempt():
    writers, guarded, problems = _scan_ls_writers()
    assert problems == []
    # the scan covers all of backend/, not only apps/
    scanned = {_module_name(p, BACKEND_DIR) for p in _production_modules(BACKEND_DIR)}
    assert {"config.urls", "config.settings.base", "manage", WRITERS_HOME} <= scanned
    assert not {m for m in scanned if ".tests." in m or ".migrations." in m}
    # the scan really sees the known writers (a broken scan must not pass vacuously)
    assert {
        "apps.billing.views.checkout_create",
        "apps.billing.views._change_plan",
        "apps.dashboard.settings_views.settings_delete",
    } <= set(writers)
    unguarded = {q for q, ok in writers.items() if not ok and q not in kill_switch.EXEMPT}
    assert unguarded == set(), (
        f"LS-writing path(s) {sorted(unguarded)} are neither @refuse_when_paused nor in "
        "kill_switch.EXEMPT — guard them, or add them to EXEMPT + operations notes"
    )
    # the policy list and the code agree in both directions
    assert guarded == set(kill_switch.GUARDED)
    assert not set(kill_switch.GUARDED) & set(kill_switch.EXEMPT)
    for qual in kill_switch.EXEMPT:
        module, _, name = qual.rpartition(".")
        assert callable(getattr(__import__(module, fromlist=[name]), name)), qual


def test_registry_scan_flags_an_unguarded_writer(tmp_path):
    """The scanner itself: a new module calling _ls_patch without the guard
    is reported; adding @refuse_when_paused clears it."""
    pkg = tmp_path / "apps" / "newapp"
    pkg.mkdir(parents=True)
    src = (
        "from apps.billing.views import _ls_patch\n\n"
        "{dec}def sneaky(request):\n    _ls_patch('/subscriptions/1', {{}})\n"
    )
    (pkg / "views.py").write_text(src.format(dec=""))
    assert _scan_ls_writers(tmp_path)[0] == {"apps.newapp.views.sneaky": False}
    (pkg / "views.py").write_text(src.format(dec="@refuse_when_paused\n"))
    assert _scan_ls_writers(tmp_path)[0] == {"apps.newapp.views.sneaky": True}


@pytest.mark.parametrize(
    "line",
    [
        'getattr(views, "_ls_patch")("/subscriptions/1", {})',
        'vars(views)["_ls_post"]("/checkouts", {})',
        'views.__dict__["_ls_delete"]("/subscriptions/1")',
        'getattr(views, "_ls_" + "patch")("/subscriptions/1", {})',
        'pkgutil.resolve_name("apps.billing.views._ls_post")("/checkouts", {})',
        'getattr(views, "_ls_send_json")("POST", "/checkouts", {}, (3, 10))',
        'pkgutil.resolve_name("apps.billing.views._ls_send_json")("PATCH", "/s/1", {}, (3, 10))',
    ],
    ids=["getattr", "vars", "dunder-dict", "split-name", "dotted-path", "getattr-edge", "dotted-edge"],
)
def test_registry_scan_flags_reflective_writer_access_outside_apps(tmp_path, line):
    """A dynamic caller is reported even outside apps/ (config/ here) and
    even in a function the name scan cannot see as a writer."""
    pkg = tmp_path / "config"
    pkg.mkdir()
    (pkg / "hooks.py").write_text(f"from apps.billing import views\n\n\ndef sneaky():\n    {line}\n")
    problems = _scan_ls_writers(tmp_path)[2]
    assert len(problems) == 1 and problems[0].startswith("config.hooks: LS write helper name as a string")


def test_registry_scan_reflection_allow_list_and_writer_home(tmp_path, monkeypatch):
    """Only WRITERS_HOME and an explicit WRITER_STRING_ALLOW entry may hold
    a writer name as a string; a test module is not production code."""
    src = 'WRITER = "_ls_post"\n'
    for rel in ("apps/billing/views.py", "config/ok.py", "apps/x/tests/test_x.py", "apps/x/test_y.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(src)
    assert _scan_ls_writers(tmp_path)[2] == [
        "config.ok: LS write helper name as a string '_ls_post' (line 1)"
    ]
    monkeypatch.setitem(WRITER_STRING_ALLOW, "config.ok", "reviewed: test double")
    assert _scan_ls_writers(tmp_path)[2] == []


# --- 3c. Edge: the money writers refuse by themselves while paused -------------


@override_settings(**OFF)
@pytest.mark.parametrize(
    "call",
    [
        lambda: bviews._ls_post("/checkouts", {"data": {}}),
        lambda: bviews._ls_patch("/subscriptions/1", {"data": {}}),
        lambda: getattr(bviews, "_ls_patch")("/subscriptions/1", {"data": {}}),  # noqa: B009
        lambda: vars(bviews)["_ls_post"]("/checkouts", {"data": {}}),
        lambda: bviews._ls_send_json("POST", "/checkouts", {}, (3, 10)),
        lambda: bviews._ls_send_json("PATCH", "/subscriptions/1", {}, (3, 10)),
        lambda: getattr(bviews, "_ls_send_json")("POST", "/checkouts", {}, (3, 10)),  # noqa: B009
        lambda: getattr(bviews, "_ls_send_json")("PATCH", "/subscriptions/1", {}, (3, 10)),  # noqa: B009
        lambda: vars(bviews)["_ls_send_json"]("POST", "/checkouts", {}, (3, 10)),
    ],
    ids=[
        "post-checkouts",
        "patch-subscription",
        "getattr-patch",
        "vars-post",
        "edge-post",
        "edge-patch",
        "getattr-edge-post",
        "getattr-edge-patch",
        "vars-edge-post",
    ],
)
def test_off_ls_money_writer_direct_call_raises_before_any_http(call):
    # BL-22: billing-lifecycle scenario (skill billing-lifecycle)
    """No decorator in the way: a direct or reflective call to the real
    writer raises BillingChangesPaused and the HTTP edge is never touched."""
    with ls_edge() as edge, pytest.raises(kill_switch.BillingChangesPaused):
        call()
    assert edge.call_count == 0


@override_settings(**ON)
@pytest.mark.parametrize("method", ["DELETE", "PUT", "GET", "post"])
def test_ls_send_json_refuses_any_method_but_post_and_patch(method):
    """Not a generic writer: even with the switch ON, any other method is a
    ValueError before any HTTP (DELETE has its own exempt _ls_delete)."""
    with ls_edge() as edge, pytest.raises(ValueError, match="POST/PATCH money writer"):
        bviews._ls_send_json(method, "/subscriptions/1", {}, (3, 10))
    assert edge.call_count == 0


@override_settings(**OFF)
@pytest.mark.parametrize("method", ["DELETE", "PUT"])
def test_off_ls_send_json_other_methods_still_raise_before_any_http(method):
    """Paused: the kill switch is the first statement, so it wins."""
    with ls_edge() as edge, pytest.raises(kill_switch.BillingChangesPaused):
        bviews._ls_send_json(method, "/subscriptions/1", {}, (3, 10))
    assert edge.call_count == 0


@override_settings(ENABLE_BILLING=True, BILLING_CHANGES_ENABLED=None)
def test_invalid_value_ls_money_writer_fails_closed_at_the_edge():
    with ls_edge() as edge, pytest.raises(kill_switch.BillingChangesPaused):
        bviews._ls_patch("/subscriptions/1", {"data": {}})
    with ls_edge() as edge, pytest.raises(kill_switch.BillingChangesPaused):
        bviews._ls_send_json("POST", "/checkouts", {}, (3, 10))
    assert edge.call_count == 0


def test_billing_changes_paused_is_not_swallowed_by_ls_failure_handlers():
    """Callers catch URLError / TimeoutError / KeyError / ValueError / OSError
    as "LS failed"; the paused exception must not be one of them."""
    assert not issubclass(kill_switch.BillingChangesPaused, (OSError, TimeoutError, KeyError, ValueError))


@override_settings(**OFF)
def test_off_ls_delete_is_exempt_and_still_reaches_ls():
    with ls_edge({("DELETE", "/subscriptions/sub_exempt"): {}}) as edge:
        bviews._ls_delete("/subscriptions/sub_exempt")
    calls = [(c.args[0].get_method(), c.args[0].full_url) for c in edge.call_args_list]
    assert calls == [("DELETE", f"{bviews.LS_API_BASE}/subscriptions/sub_exempt")]


@override_settings(**ON)
def test_on_ls_money_writers_reach_ls():
    routes = {("POST", "/checkouts"): {"ok": 1}, ("PATCH", "/subscriptions/1"): {"ok": 2}}
    with ls_edge(routes) as edge:
        assert bviews._ls_post("/checkouts", {"data": {}}) == {"ok": 1}
        assert bviews._ls_patch("/subscriptions/1", {"data": {}}) == {"ok": 2}
        assert bviews._ls_send_json("POST", "/checkouts", {}, (3, 10)) == {"ok": 1}
    assert [c.args[0].get_method() for c in edge.call_args_list] == ["POST", "PATCH", "POST"]
