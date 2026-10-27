"""Hand-built snapshots: tolerant of JSON scalars, loud on broken structure.

A :class:`~niwaki.snapshot.Snapshot` is a plain JSON document.  ``take()``
writes every attribute value as a wire string, but a snapshot can also be
written by hand, loaded from a file another tool produced, or edited — and
there an attribute is naturally ``True``, ``1`` or ``None``.  Two contracts:

* **Scalar tolerance** (the behaviour the SDK always had, measured on the
  pre-typing code): ``to_design`` and ``snapshot.diff`` accept any JSON scalar
  as an attribute value.  ``to_design`` coerces it through the typed model
  exactly as it would coerce the wire string (``True`` ≡ ``"yes"``,
  ``9000`` ≡ ``"9000"``, ``None`` ≡ absent); ``diff`` compares values verbatim
  and reports them as they are written (``(True, "no")``).
* **Structural malformation is a documented error, never a raw crash.**  A
  node that is not an object, a node without ``class``, ``children`` or
  ``attributes`` of the wrong JSON type:

  - ``snapshot.diff`` raises :class:`ValueError` (a subclass is fine) naming
    the problem;
  - ``design.to_design`` keeps its collect-all contract: it raises
    :class:`~niwaki.exceptions.SnapshotImportError` listing one
    ``"structure"`` problem per malformed node — alongside every other
    problem of the same tree — and never a bare ``TypeError`` / ``KeyError``.
"""

from __future__ import annotations

import pytest

from niwaki._json import JsonObject, JsonValue
from niwaki.design import to_design
from niwaki.exceptions import SnapshotImportError
from niwaki.snapshot import Snapshot, diff

_BD_DN = "uni/tn-prod/BD-web"


def _snapshot(children: list[JsonValue]) -> Snapshot:
    """A ``uni`` snapshot holding tenant ``prod`` with *children*."""
    tenant: JsonObject = {
        "class": "fvTenant",
        "rn": "tn-prod",
        "attributes": {"name": "prod"},
        "children": children,
    }
    return Snapshot(
        scope="uni",
        tree={"class": "polUni", "rn": "uni", "attributes": {}, "children": [tenant]},
    )


def _bd(attrs: JsonObject) -> Snapshot:
    """A snapshot with one bridge domain ``web`` carrying *attrs*."""
    bd: JsonObject = {
        "class": "fvBD",
        "rn": "BD-web",
        "attributes": {"name": "web", **attrs},
        "children": [],
    }
    return _snapshot([bd])


def _bd_payload(snapshot: Snapshot) -> object:
    """The BD envelope the imported design would push."""
    payload = to_design(snapshot).to_payload()
    uni = payload["polUni"]
    assert isinstance(uni, dict)
    children = uni["children"]
    assert isinstance(children, list)
    tenant = children[0]
    assert isinstance(tenant, dict)
    inner = tenant["fvTenant"]
    assert isinstance(inner, dict)
    bds = inner["children"]
    assert isinstance(bds, list)
    return bds[0]


# ── scalar tolerance: to_design ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scalar", "wire"),
    [
        ({"arpFlood": True}, {"arpFlood": "yes"}),
        ({"arpFlood": False}, {"arpFlood": "no"}),
        ({"arpFlood": 1}, {"arpFlood": "yes"}),
        ({"arpFlood": 1.0}, {"arpFlood": "yes"}),
        ({"mtu": 9000}, {"mtu": "9000"}),
        ({"descr": None}, {}),
    ],
    ids=["bool-true", "bool-false", "int-as-bool", "float-as-bool", "int", "null"],
)
def test_to_design_accepts_json_scalars_like_their_wire_strings(
    scalar: JsonObject, wire: JsonObject
) -> None:
    assert _bd_payload(_bd(scalar)) == _bd_payload(_bd(wire))


def test_to_design_scalar_payload_matches_the_pre_typing_output() -> None:
    """The literal output measured on the pre-typing code, pinned."""
    assert _bd_payload(_bd({"arpFlood": True, "mtu": 9000})) == {
        "fvBD": {"attributes": {"name": "web", "arpFlood": "true", "mtu": "9000"}}
    }


# ── scalar tolerance: diff ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "other", "expected"),
    [
        (True, "no", (True, "no")),
        (1, "yes", (1, "yes")),
        (1.5, "1.5", (1.5, "1.5")),
        (False, None, (False, None)),
    ],
    ids=["bool", "int", "float", "bool-vs-absent"],
)
def test_diff_reports_scalars_verbatim(
    value: JsonValue, other: str | None, expected: tuple[JsonValue, JsonValue]
) -> None:
    a = _bd({"arpFlood": value})
    b = _bd({} if other is None else {"arpFlood": other})
    assert diff(a, b).changed == {_BD_DN: {"arpFlood": expected}}


@pytest.mark.parametrize("value", [True, 0, 2.5, None], ids=["bool", "int", "float", "null"])
def test_diff_of_identical_scalar_snapshots_is_empty(value: JsonValue) -> None:
    assert not diff(_bd({"arpFlood": value}), _bd({"arpFlood": value})).has_changes


def test_diff_null_and_absent_are_the_same() -> None:
    """Measured on the pre-typing code: ``None`` reads like a missing attribute."""
    assert not diff(_bd({"descr": None}), _bd({})).has_changes


# ── structural malformation ──────────────────────────────────────────────────

_GOOD_BD: JsonObject = {
    "class": "fvBD",
    "rn": "BD-ok",
    "attributes": {"name": "ok"},
    "children": [],
}

_MALFORMED: dict[str, JsonValue] = {
    "node-not-an-object": "junk",
    "node-is-a-list": [1, 2],
    "missing-class": {"rn": "BD-x", "attributes": {"name": "x"}, "children": []},
    "class-not-a-string": {"class": 7, "rn": "BD-x", "attributes": {}, "children": []},
    "children-not-a-list": {
        "class": "fvBD",
        "rn": "BD-x",
        "attributes": {"name": "x"},
        "children": "nope",
    },
    "attributes-not-an-object": {
        "class": "fvBD",
        "rn": "BD-x",
        "attributes": ["name", "x"],
        "children": [],
    },
}


@pytest.mark.parametrize("node", list(_MALFORMED.values()), ids=list(_MALFORMED))
def test_diff_rejects_malformed_structure_with_value_error(node: JsonValue) -> None:
    broken = _snapshot([node, _GOOD_BD])
    with pytest.raises(ValueError, match=r"\S"):
        diff(broken, broken)


def test_diff_rejects_a_root_whose_children_is_not_a_list() -> None:
    broken = Snapshot(
        scope="uni", tree={"class": "polUni", "rn": "uni", "attributes": {}, "children": 5}
    )
    with pytest.raises(ValueError, match=r"\S"):
        diff(broken, broken)


@pytest.mark.parametrize("node", list(_MALFORMED.values()), ids=list(_MALFORMED))
def test_to_design_reports_malformed_structure_as_an_import_problem(node: JsonValue) -> None:
    with pytest.raises(SnapshotImportError) as exc_info:
        to_design(_snapshot([node, _GOOD_BD]))
    problems = exc_info.value.problems
    assert [p.kind for p in problems] == ["structure"]
    assert problems[0].dn.startswith("uni/tn-prod")
    assert problems[0].detail


def test_to_design_collects_every_problem_with_the_malformed_ones() -> None:
    """Malformation joins the collect-all report; it does not short-circuit it."""
    unknown: JsonObject = {
        "class": "fvNoSuchClass",
        "rn": "nosuch-x",
        "attributes": {},
        "children": [],
    }
    tree = _snapshot([_MALFORMED["node-not-an-object"], unknown, _MALFORMED["missing-class"]])
    with pytest.raises(SnapshotImportError) as exc_info:
        to_design(tree)
    kinds = sorted(p.kind for p in exc_info.value.problems)
    assert kinds == ["structure", "structure", "unknown-class"]


def test_to_design_reports_a_malformed_root() -> None:
    broken = Snapshot(
        scope="uni", tree={"class": "polUni", "rn": "uni", "attributes": {}, "children": 5}
    )
    with pytest.raises(SnapshotImportError) as exc_info:
        to_design(broken)
    assert [p.kind for p in exc_info.value.problems] == ["structure"]
