"""``catalog.relation()`` / ``catalog.relations()`` — the relation graph, offline.

The shipped catalogue's ``relation`` table describes every Rs class: which class
carries it (``from_mo``), which class it points at (``to_mo``), the Rt class the
target grows in return, its cardinality, its kind, and two behaviour flags.
These tests pin the public door to that table:

* ``relation(rs_class)`` — one Rs class described as a :class:`RelationDoc`.
* ``relations(class_name, *, direction="out" | "in")`` — every Rs a class can
  carry (``out``) or be the target of (``in``), inheritance included both ways.

What the table holds, measured on the shipped 6.0(9c) catalogue (3,027 rows)
and pinned here because it shapes the contract:

* **1,499 Rs rows** — keyed by the Rs class itself (``rs_class`` equals the
  wire form of ``from_rel_mo``).  These, and only these, are relations.
* **1,487 Rt rows** — the *same* relation keyed by its Rt class (``rs_class``
  equals the wire form of ``to_rel_mo``), field-for-field identical to the Rs
  row.  An Rt class is not an Rs: ``relation()`` refuses it, and ``relations()``
  never reports a relation twice.
* **41 abstract relation bases** (``relnFrom``, ``fvARsToRemote`` …) whose row
  carries no endpoint at all.  Not relations either: ``relation()`` refuses them.
* Endpoints are stored as ``pkg:Name`` and served as wire names
  (``fv:Ctx`` → ``fvCtx``) — the conversion the rest of the catalogue uses.
* A source or target may be **abstract** (``fvRsCtx`` hangs off ``fvABDPol``;
  ``fvRsDomAtt`` points at ``infraDomP``), which is why ``relations()`` walks
  the class's ancestors: an Rs carried by an abstract holder applies to every
  concrete descendant, and an Rs aimed at an abstract target reaches every
  concrete descendant of it.
* A few endpoints are cloud classes absent from this catalogue (``fvRsCloudAccount``
  → ``cloudAccount``): ``relation()`` still serves the Rs, and ``relations()``
  of the unknown endpoint raises like any unknown class.

Everything here reads the shipped ``catalog.db`` — no corpus, no APIC.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import textwrap
import threading
from collections import defaultdict
from pathlib import Path

import pytest

from niwaki import catalog
from niwaki.exceptions import UnknownClassError
from niwaki.query import _catalog

_ROOT = Path(__file__).resolve().parents[2]
_API_PAGE = _ROOT / "docs" / "reference" / "api" / "catalog.md"

_FIELDS = {
    "rs_class",
    "source",
    "target",
    "target_rt",
    "cardinality",
    "kind",
    "enforceable",
    "resolvable",
}
_CARDINALITIES = {"1-to-1", "1-to-m", "n-to-1", "n-to-m"}
_KINDS = {"explicit", "named"}


# ── an independent reading of the raw tables — the oracle ────────────────────

_RawRow = tuple[
    str, str | None, str | None, str | None, str | None, str | None, str | None, int, int
]


def _wire(name: str) -> str:
    """``pkg:Name`` → ``pkgName``, the catalogue-wide conversion."""
    return name.replace(":", "")


@dataclasses.dataclass(frozen=True)
class _Oracle:
    """The relation graph recomputed straight from ``catalog.db``.

    Deliberately written against the raw rows (not the reader's API) so the
    public functions are checked against an independent derivation.
    """

    rs_rows: dict[str, _RawRow]  # rs_class -> row, Rs-keyed rows only
    other_keys: frozenset[str]  # Rt rows + abstract relation bases
    known: frozenset[str]  # every class the catalogue knows
    ancestors: dict[str, frozenset[str]]  # class -> transitive superclasses (wire)
    by_source: dict[str, tuple[str, ...]]  # wire source class -> Rs declared on it
    by_target: dict[str, tuple[str, ...]]  # wire target class -> Rs aimed at it

    def doc(self, rs_class: str) -> catalog.RelationDoc:
        row = self.rs_rows[rs_class]
        _, rel_type, cardinality, from_mo, _, to_mo, to_rel_mo, enforceable, resolvable = row
        assert rel_type and cardinality and from_mo and to_mo and to_rel_mo
        return catalog.RelationDoc(
            rs_class=rs_class,
            source=_wire(from_mo),
            target=_wire(to_mo),
            target_rt=_wire(to_rel_mo),
            cardinality=cardinality,
            kind=rel_type,
            enforceable=bool(enforceable),
            resolvable=bool(resolvable),
        )

    def lineage(self, class_name: str) -> frozenset[str]:
        return self.ancestors.get(class_name, frozenset()) | {class_name}

    def expected(self, class_name: str, direction: str) -> tuple[str, ...]:
        """Sorted Rs class names ``relations(class_name, direction)`` must return."""
        index = self.by_source if direction == "out" else self.by_target
        return tuple(
            sorted(rs for owner in self.lineage(class_name) for rs in index.get(owner, ()))
        )


@pytest.fixture(scope="module")
def oracle() -> _Oracle:
    con = _catalog.catalog()._connection
    rows: list[_RawRow] = list(
        con.execute(
            "SELECT rs_class, rel_type, cardinality, from_mo, from_rel_mo, "
            "to_mo, to_rel_mo, enforceable, resolvable FROM relation"
        )
    )
    rs_rows = {r[0]: r for r in rows if r[4] is not None and _wire(r[4]) == r[0]}
    other_keys = frozenset(r[0] for r in rows if r[0] not in rs_rows)

    names: dict[int, str] = {}
    for class_id, class_name in con.execute("SELECT id, class_name FROM mo"):
        names[int(class_id)] = str(class_name)
    direct: defaultdict[str, set[str]] = defaultdict(set)
    for class_id, ancestor in con.execute("SELECT class_id, ancestor FROM inherits"):
        direct[names[int(class_id)]].add(_wire(str(ancestor)))

    def closure(class_name: str) -> frozenset[str]:
        seen: set[str] = set()
        stack = list(direct[class_name])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(direct.get(current, ()))
        return frozenset(seen)

    by_source: defaultdict[str, list[str]] = defaultdict(list)
    by_target: defaultdict[str, list[str]] = defaultdict(list)
    for rs, row in rs_rows.items():
        from_mo, to_mo = row[3], row[5]
        assert from_mo is not None and to_mo is not None, rs
        by_source[_wire(from_mo)].append(rs)
        by_target[_wire(to_mo)].append(rs)

    return _Oracle(
        rs_rows=rs_rows,
        by_source={k: tuple(v) for k, v in by_source.items()},
        by_target={k: tuple(v) for k, v in by_target.items()},
        other_keys=other_keys,
        known=frozenset(names.values()),
        ancestors={name: closure(name) for name in names.values()},
    )


def _names(docs: tuple[catalog.RelationDoc, ...]) -> tuple[str, ...]:
    return tuple(doc.rs_class for doc in docs)


# ── the public surface ────────────────────────────────────────────────────────


def test_the_new_names_are_public() -> None:
    for name in ("RelationDoc", "relation", "relations"):
        assert name in catalog.__all__, f"{name} missing from niwaki.catalog.__all__"
        assert hasattr(catalog, name)


def test_relation_doc_is_a_frozen_dataclass_with_exactly_the_contract_fields() -> None:
    doc_type = catalog.RelationDoc
    assert dataclasses.is_dataclass(doc_type)
    assert {f.name for f in dataclasses.fields(doc_type)} == _FIELDS

    doc = catalog.relation("fvRsCtx")
    for field in _FIELDS:
        with pytest.raises(AttributeError):
            setattr(doc, field, "tampered")


def test_relation_doc_is_hashable_and_compares_by_value() -> None:
    assert catalog.relation("fvRsCtx") == catalog.relation("fvRsCtx")
    assert hash(catalog.relation("fvRsCtx")) == hash(catalog.relation("fvRsCtx"))
    assert catalog.relation("fvRsCtx") != catalog.relation("fvRsBd")


def test_the_api_page_documents_the_new_names() -> None:
    page = _API_PAGE.read_text(encoding="utf-8")
    assert ".. autofunction:: niwaki.catalog.relation\n" in page
    assert ".. autofunction:: niwaki.catalog.relations\n" in page
    assert ".. autoclass:: niwaki.catalog.RelationDoc" in page


def test_every_public_catalog_name_is_on_the_api_page() -> None:
    """The reference page lists the module member by member (``:no-members:``),
    so a name added to ``__all__`` without its directive ships undocumented."""
    page = _API_PAGE.read_text(encoding="utf-8")
    missing = [name for name in catalog.__all__ if f"niwaki.catalog.{name}\n" not in page]
    assert missing == [], f"public names without a directive on {_API_PAGE.name}: {missing}"


@pytest.mark.parametrize("name", ["relation", "relations"])
def test_the_public_functions_carry_verbose_docstrings(name: str) -> None:
    doc = getattr(catalog, name).__doc__
    assert doc is not None
    for section in ("Args:", "Returns:", "Raises:", "Example"):
        assert section in doc, f"catalog.{name} docstring lacks {section!r}"


def test_relation_doc_carries_a_docstring() -> None:
    assert catalog.RelationDoc.__doc__
    assert "rs_class" in catalog.RelationDoc.__doc__


# ── relation(rs_class) ────────────────────────────────────────────────────────


def test_relation_describes_a_named_rs_carried_by_an_abstract_holder() -> None:
    """``fvRsCtx`` is declared on ``fvABDPol`` — the abstract BD policy — not on
    ``fvBD``: the source is served as the schema states it, never rewritten."""
    assert catalog.relation("fvRsCtx") == catalog.RelationDoc(
        rs_class="fvRsCtx",
        source="fvABDPol",
        target="fvCtx",
        target_rt="fvRtCtx",
        cardinality="n-to-1",
        kind="named",
        enforceable=True,
        resolvable=True,
    )


def test_relation_describes_an_epg_to_bridge_domain_binding() -> None:
    assert catalog.relation("fvRsBd") == catalog.RelationDoc(
        rs_class="fvRsBd",
        source="fvAEPg",
        target="fvBD",
        target_rt="fvRtBd",
        cardinality="n-to-1",
        kind="named",
        enforceable=True,
        resolvable=True,
    )


def test_relation_describes_an_explicit_rs_aimed_at_an_abstract_target() -> None:
    """Domain attachment points at ``infraDomP`` — abstract; physical, L3, VMM …
    domains are its concrete descendants."""
    assert catalog.relation("fvRsDomAtt") == catalog.RelationDoc(
        rs_class="fvRsDomAtt",
        source="fvAEPg",
        target="infraDomP",
        target_rt="infraRtDomAtt",
        cardinality="n-to-m",
        kind="explicit",
        enforceable=True,
        resolvable=True,
    )
    assert catalog.describe("infraDomP").is_abstract


def test_relation_flags_are_independent_booleans() -> None:
    """Both ``False`` on a static path; enforceable but not resolvable on a VLAN
    pool binding — the two flags are served as they stand, not derived."""
    path = catalog.relation("fvRsPathAtt")
    assert (path.kind, path.cardinality) == ("explicit", "n-to-m")
    assert (path.source, path.target, path.target_rt) == (
        "fvAEPg",
        "fabricPathEp",
        "fabricRtPathAtt",
    )
    assert path.enforceable is False
    assert path.resolvable is False

    vlan = catalog.relation("infraRsVlanNs")
    assert (vlan.source, vlan.target, vlan.target_rt) == (
        "infraADomP",
        "fvnsVlanInstP",
        "fvnsRtVlanNs",
    )
    assert vlan.enforceable is True
    assert vlan.resolvable is False


def test_relation_flags_are_real_bools_not_sqlite_integers() -> None:
    doc = catalog.relation("fvRsCtx")
    assert type(doc.enforceable) is bool
    assert type(doc.resolvable) is bool


def test_relation_serves_an_rs_whose_target_this_catalogue_does_not_know() -> None:
    """A tenant's cloud-account binding aims at a cloud class absent from this
    catalogue: the Rs is still described, the target name still wire-converted."""
    doc = catalog.relation("fvRsCloudAccount")
    assert doc.source == "fvTenant"
    assert doc.target == "cloudAccount"
    with pytest.raises(UnknownClassError):
        catalog.describe(doc.target)


def test_relation_of_an_unknown_class_raises() -> None:
    with pytest.raises(UnknownClassError, match="fvRsNoSuchThing"):
        catalog.relation("fvRsNoSuchThing")
    with pytest.raises(KeyError):  # like every catalogue lookup
        catalog.relation("fvRsNoSuchThing")


def test_relation_of_a_known_class_that_is_not_an_rs_raises_explicitly() -> None:
    """Same error family as a typo, but the message says *why*: the class exists
    and is simply not a relation."""
    catalog.describe("fvBD")  # known
    with pytest.raises(UnknownClassError) as caught:
        catalog.relation("fvBD")
    message = str(caught.value)
    assert "fvBD" in message
    assert "relation" in message.lower()


def test_relation_of_an_rt_class_raises() -> None:
    """``fvRtCtx`` has its own row in the table (the relation seen from the
    target), but an Rt is not an Rs: describe the relation by ``fvRsCtx``."""
    catalog.describe("fvRtCtx")  # known
    with pytest.raises(UnknownClassError, match="fvRtCtx"):
        catalog.relation("fvRtCtx")


def test_relation_of_an_abstract_relation_base_raises() -> None:
    """``relnFrom`` is the abstract root of every Rs — no endpoints, no relation."""
    assert catalog.describe("relnFrom").is_abstract
    with pytest.raises(UnknownClassError, match="relnFrom"):
        catalog.relation("relnFrom")


# ── relations(class_name, *, direction) ──────────────────────────────────────


def test_relations_out_of_a_bridge_domain() -> None:
    """Every one of these is declared on the abstract ``fvABDPol`` — none on
    ``fvBD`` itself — so this is inheritance or nothing."""
    assert _names(catalog.relations("fvBD", direction="out")) == (
        "fvRsABDPolMonPol",
        "fvRsBDToFhs",
        "fvRsBDToNdP",
        "fvRsBDToNetflowMonitorPol",
        "fvRsBDToOut",
        "fvRsBDToProfile",
        "fvRsBDToRelayP",
        "fvRsBdFloodTo",
        "fvRsBdToEpRet",
        "fvRsCtx",
        "fvRsIgmpsn",
        "fvRsMldsn",
    )


def test_relations_in_to_a_bridge_domain() -> None:
    """Includes the three Rs aimed at the abstract ``fvABDPol``."""
    assert _names(catalog.relations("fvBD", direction="in")) == (
        "fvRsBd",
        "fvRsEpTagBd",
        "fvRsSvcBDToBDAtt",
        "infraRsInfraBD",
        "l2extRsEBd",
        "mgmtRsMgmtBD",
        "presRsDefInfraBd",
        "spanRsSrcToBD",
        "vnsRsBDDefToBD",
        "vnsRsEPpInfoToBD",
        "vnsRsLIfCtxToBD",
        "vnsRsREPpInfoToBD",
    )


def test_direction_defaults_to_out() -> None:
    assert catalog.relations("fvBD") == catalog.relations("fvBD", direction="out")


def test_relations_returns_relation_docs_equal_to_relation() -> None:
    docs = catalog.relations("fvAEPg", direction="out")
    assert isinstance(docs, tuple)
    assert docs
    for doc in docs:
        assert isinstance(doc, catalog.RelationDoc)
        assert doc == catalog.relation(doc.rs_class)


def test_out_includes_rs_carried_by_an_abstract_superclass() -> None:
    names = _names(catalog.relations("fvAEPg", direction="out"))
    assert "fvRsBd" in names  # declared on fvAEPg itself
    assert "fvRsProv" in names  # declared on the abstract fvEPg
    assert "fvRsCons" in names


def test_out_does_not_reach_down_into_subclasses() -> None:
    """``fvEPg`` is abstract; ``fvRsBd`` belongs to its subclass ``fvAEPg``.  An
    abstract class does not carry what only some of its descendants carry."""
    names = _names(catalog.relations("fvEPg", direction="out"))
    assert "fvRsProv" in names
    assert "fvRsBd" not in names


def test_in_reaches_a_concrete_class_through_an_abstract_target() -> None:
    """``fvRsDomAtt`` targets the abstract ``infraDomP``: every concrete domain
    is reachable, and so is the abstract target itself."""
    for domain in ("physDomP", "l3extDomP", "infraDomP"):
        assert "fvRsDomAtt" in _names(catalog.relations(domain, direction="in")), domain


def test_in_to_a_vrf_contains_the_bridge_domain_binding() -> None:
    assert "fvRsCtx" in _names(catalog.relations("fvCtx", direction="in"))


def test_in_does_not_reach_up_into_subclasses_of_the_target() -> None:
    """``fvRsBd`` targets the concrete ``fvBD``; the abstract ``fvABDPol`` above
    it is not a valid target of it."""
    assert "fvRsBd" not in _names(catalog.relations("fvABDPol", direction="in"))


def test_a_class_with_no_relation_returns_empty_tuples() -> None:
    for class_name in ("fvRsCtx", "topRoot"):
        assert catalog.relations(class_name, direction="out") == ()
        assert catalog.relations(class_name, direction="in") == ()


def test_a_class_with_only_one_direction() -> None:
    assert catalog.relations("fvSubnet", direction="in") == ()
    assert _names(catalog.relations("fvSubnet", direction="out")) == (
        "fvRsBDSubnetToOut",
        "fvRsBDSubnetToProfile",
        "fvRsNdPfxPol",
    )


def test_an_rs_appears_once_even_though_the_table_also_holds_its_rt_row() -> None:
    """``fvRtCtx``'s row repeats ``fvRsCtx``; a naive scan of the table by
    ``to_mo`` would report the relation twice."""
    names = _names(catalog.relations("fvCtx", direction="in"))
    assert names.count("fvRsCtx") == 1
    assert "fvRtCtx" not in names
    assert len(names) == len(set(names))


@pytest.mark.parametrize("direction", ["out", "in"])
def test_relations_of_an_unknown_class_raises(direction: str) -> None:
    with pytest.raises(UnknownClassError, match="fvNoSuchThing"):
        catalog.relations("fvNoSuchThing", direction=direction)  # type: ignore[arg-type]


def test_relations_of_an_endpoint_absent_from_this_catalogue_raises() -> None:
    target = catalog.relation("fvRsCloudAccount").target
    with pytest.raises(UnknownClassError):
        catalog.relations(target, direction="in")


@pytest.mark.parametrize("direction", ["sideways", "OUT", "", "both"])
def test_an_invalid_direction_raises_value_error(direction: str) -> None:
    with pytest.raises(ValueError, match="direction"):
        catalog.relations("fvBD", direction=direction)  # type: ignore[arg-type]


def test_an_invalid_direction_is_refused_before_the_class_is_looked_up() -> None:
    """A bad argument is a programming error whatever the class — a typo in the
    class name must not mask it as a lookup failure."""
    with pytest.raises(ValueError, match="direction"):
        catalog.relations("fvNoSuchThing", direction="sideways")  # type: ignore[arg-type]


def test_direction_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        catalog.relations("fvBD", "in")  # type: ignore[misc]


def test_relations_is_memoised_per_class_and_direction() -> None:
    assert catalog.relations("fvBD", direction="out") is catalog.relations("fvBD", direction="out")
    assert catalog.relations("fvBD", direction="in") is catalog.relations("fvBD", direction="in")
    assert catalog.relations("fvBD") is catalog.relations("fvBD", direction="out")


# ── whole-table properties ────────────────────────────────────────────────────


def test_the_table_has_the_measured_shape(oracle: _Oracle) -> None:
    """The counts the module docstring states — a schema bump that changes the
    table's structure (not just its size) should be looked at, not absorbed."""
    assert len(oracle.rs_rows) == 1499
    assert len(oracle.other_keys) == 1487 + 41


def test_every_rs_row_is_served_by_relation(oracle: _Oracle) -> None:
    wrong = [rs for rs in oracle.rs_rows if catalog.relation(rs) != oracle.doc(rs)]
    assert wrong == [], f"{len(wrong)} relation() mismatches, e.g. {wrong[:5]}"


def test_every_served_relation_has_a_contract_cardinality_and_kind(oracle: _Oracle) -> None:
    for rs in oracle.rs_rows:
        doc = catalog.relation(rs)
        assert doc.cardinality in _CARDINALITIES, rs
        assert doc.kind in _KINDS, rs
        assert ":" not in doc.source + doc.target + doc.target_rt, rs


def test_no_rt_row_or_abstract_base_is_served_by_relation(oracle: _Oracle) -> None:
    served: list[str] = []
    for key in oracle.other_keys:
        try:
            catalog.relation(key)
        except UnknownClassError:
            continue
        served.append(key)
    assert served == [], f"non-Rs rows served as relations: {sorted(served)[:5]}"


def test_every_rs_is_reachable_from_both_of_its_endpoints(oracle: _Oracle) -> None:
    """The completeness property: no relation is invisible to ``relations()``
    from its source, from its target, or from any concrete descendant of an
    abstract source or target.  Endpoints this catalogue does not know (a few
    cloud classes) are the only exemption."""
    descendants: defaultdict[str, set[str]] = defaultdict(set)
    for class_name, ancestors in oracle.ancestors.items():
        for ancestor in ancestors:
            descendants[ancestor].add(class_name)

    out_of: dict[str, frozenset[str]] = {}
    in_of: dict[str, frozenset[str]] = {}

    def reach(table: dict[str, frozenset[str]], class_name: str, direction: str) -> frozenset[str]:
        if class_name not in table:
            docs = catalog.relations(class_name, direction=direction)  # type: ignore[arg-type]
            table[class_name] = frozenset(_names(docs))
        return table[class_name]

    missing: list[tuple[str, str, str]] = []
    for rs in oracle.rs_rows:
        doc = catalog.relation(rs)
        for endpoint, direction, table in (
            (doc.source, "out", out_of),
            (doc.target, "in", in_of),
        ):
            if endpoint not in oracle.known:
                continue
            for class_name in {endpoint} | descendants[endpoint]:
                if rs not in reach(table, class_name, direction):
                    missing.append((rs, direction, class_name))
    assert missing == [], f"{len(missing)} unreachable (rs, direction, class), e.g. {missing[:5]}"


@pytest.mark.parametrize("direction", ["out", "in"])
def test_relations_is_exactly_the_inherited_view_for_every_class(
    oracle: _Oracle, direction: str
) -> None:
    """Soundness as well as completeness, on every class the catalogue knows:
    nothing missing, nothing extra, sorted by ``rs_class``."""
    wrong: list[str] = []
    for class_name in sorted(oracle.known):
        got = _names(catalog.relations(class_name, direction=direction))  # type: ignore[arg-type]
        if got != oracle.expected(class_name, direction):
            wrong.append(class_name)
    assert wrong == [], f"{len(wrong)} classes disagree ({direction}), e.g. {wrong[:5]}"


# ── offline, lazy, thread-safe ───────────────────────────────────────────────


def test_import_opens_nothing_and_relations_run_without_a_network() -> None:
    """A fresh interpreter: importing the package and the catalogue module opens
    no catalogue; the first ``relations()`` call opens it through the single
    reader — with every socket refused, so nothing but the shipped file is read."""
    script = textwrap.dedent(
        """
        import socket

        def _refuse(*args, **kwargs):
            raise OSError("network access attempted")

        socket.socket.connect = _refuse
        socket.create_connection = _refuse

        import niwaki
        import niwaki.catalog
        from niwaki.query import _catalog

        assert _catalog._instance is None, "catalogue opened at import time"
        names = [d.rs_class for d in niwaki.catalog.relations("fvBD", direction="in")]
        assert "fvRsBd" in names, names
        assert niwaki.catalog.relation("fvRsCtx").target == "fvCtx"
        assert _catalog._instance is not None, "relations() bypassed the catalogue reader"
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=_ROOT,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_concurrent_callers_never_receive_another_class_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the ``dn_formats`` guard: one shared connection, many threads,
    first builds racing — every caller must get its own class's answer.  The
    race runs on a fresh reader so it hits cold memoisation, not a warm cache."""
    classes = ("fvBD", "fvCtx", "fvAEPg", "fvEPg", "physDomP", "fvTenant", "fvSubnet", "fvABDPol")
    expected = {
        (name, direction): _names(catalog.relations(name, direction=direction))
        for name in classes
        for direction in ("out", "in")
    }
    fresh = _catalog.Catalog()
    monkeypatch.setattr(_catalog, "_instance", fresh)

    wrong: list[tuple[str, str]] = []
    failures: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(len(classes) * 2)

    def hammer(name: str, direction: str) -> None:
        barrier.wait()
        for _ in range(200):
            try:
                got = _names(catalog.relations(name, direction=direction))  # type: ignore[arg-type]
            except Exception as exc:
                with guard:
                    failures.append(type(exc).__name__)
                continue
            if got != expected[(name, direction)]:
                with guard:
                    wrong.append((name, direction))

    threads = [
        threading.Thread(target=hammer, args=(name, direction))
        for name in classes
        for direction in ("out", "in")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    fresh.close()
    assert wrong == [], f"{len(wrong)} caller(s) received another class's relations"
    assert failures == [], f"concurrent callers raised: {set(failures)}"
