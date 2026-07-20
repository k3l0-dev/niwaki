"""Tests for the runtime read-catalogue reader (``niwaki.query._catalog``).

The naming-parity and metadata tests need the catalogue ``.db`` built from the
raw schemas (gitignored), so they build one into a temp file and skip when the
corpus is absent.  The lazy/error tests run everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from niwaki._codegen import generate_catalog as gc
from niwaki.query import _catalog

CORPUS_PRESENT = gc.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


def test_missing_db_raises_a_clear_error(tmp_path: Path) -> None:
    cat = _catalog.Catalog(tmp_path / "absent.db")
    with pytest.raises(FileNotFoundError, match="read catalogue not found"):
        cat.class_meta("fvBD")


@pytest.fixture(scope="module")
def cat(tmp_path_factory: pytest.TempPathFactory) -> _catalog.Catalog:
    out = tmp_path_factory.mktemp("cat") / "catalog.db"
    gc.build_catalog(out=out)
    return _catalog.Catalog(out)


@needs_corpus
def test_class_meta_exposes_readable_names_and_kinds(cat: _catalog.Catalog) -> None:
    meta = cat.class_meta("fvBD")
    # A label-derived rename, both directions.
    assert meta.readable_to_wire["arp_flooding"] == "arpFlood"
    assert meta.wire_to_readable["arpFlood"] == "arp_flooding"
    # Coercion hint from the base type.
    assert meta.wire_to_kind["arpFlood"] == "bool"
    # Naming prop tracked.
    assert "name" in meta.naming


@needs_corpus
def test_operational_class_and_unclassified_kind(cat: _catalog.Catalog) -> None:
    meta = cat.class_meta("faultInst")  # not a generated model
    assert meta.readable_to_wire  # it has readable names
    # The structural ``rn`` property is reference:BinRN — beyond the model set.
    assert meta.wire_to_kind.get("rn") is None


@needs_corpus
def test_class_meta_is_memoised(cat: _catalog.Catalog) -> None:
    first = cat.class_meta("fvBD")
    assert cat.class_meta("fvBD") is first  # same object, no second build


@needs_corpus
def test_class_meta_exposes_is_stat(cat: _catalog.Catalog) -> None:
    # A real granularity-variant stats class shipped in the corpus.
    assert cat.class_meta("acllogFlowCounter15min").is_stat is True
    assert cat.class_meta("fvBD").is_stat is False


@needs_corpus
def test_naming_parity_with_the_generated_model(cat: _catalog.Catalog) -> None:
    """The catalogue names a class exactly as its generated model does.

    The anti-"two worlds" guarantee: same ``resolve_py_names``, same inputs
    (labels + scopemeta), so every aliased field agrees.
    """
    from niwaki.models._generated.fv.fvBD import fvBD

    alias_map = fvBD._get_alias_map()  # {wire: python_name}, aliased props only
    meta = cat.class_meta("fvBD")
    for wire, python_name in alias_map.items():
        assert meta.wire_to_readable[wire] == python_name, wire


@needs_corpus
def test_naming_parity_on_a_scopemeta_class(cat: _catalog.Catalog) -> None:
    """A class whose names depend on scopemeta still matches its model."""
    try:
        from niwaki.models._generated.aaa.aaaAuthRealm import aaaAuthRealm as model
    except ImportError:
        pytest.skip("aaaAuthRealm not generated")
    meta = cat.class_meta("aaaAuthRealm")
    for wire, python_name in model._get_alias_map().items():
        assert meta.wire_to_readable[wire] == python_name, wire


# The catalogue resolves name collisions over a class's whole readable property
# set, the generator over its configurable subset, so a handful of properties
# land a different readable name in the catalogue than on their model.  Invisible
# at runtime (a generated class is served by its model).  Pinned exhaustively: a
# NEW divergence (firmware drift) breaks the build; a resolved one does too, so
# the allowlist is kept honest.  Measured on APIC 6.0(9c).
_KNOWN_NAMING_DIVERGENCES = frozenset(
    {
        ("l3extMember", "addr"),
        ("l3extOut", "enforceRtctrl"),
        ("l3extRsNodeL3OutAtt", "rtrId"),
        ("l3extRsPathL3OutAtt", "addr"),
        ("l3extRsPathL3OutAtt", "llAddr"),
        ("l3extRsPathL3OutAtt", "mac"),
        ("vmmAgtStatus", "operSt"),
        ("vmmPlInf", "state"),
        ("vnsCMgmt", "gateway"),
        ("vnsCMgmt", "host"),
        ("vnsCMgmt", "subnetmask"),
    }
)


@needs_corpus
def test_naming_parity_is_exhaustive_with_pinned_divergences(cat: _catalog.Catalog) -> None:
    """Across EVERY generated class, catalogue names match the model — bar a pinned set."""
    import importlib

    gen_root = Path("src/niwaki/models/_generated")
    divergences: set[tuple[str, str]] = set()
    for pyfile in sorted(gen_root.rglob("*.py")):
        if pyfile.stem.startswith("_"):
            continue
        try:
            parts = pyfile.relative_to(gen_root).with_suffix("").parts
            model = getattr(
                importlib.import_module("niwaki.models._generated." + ".".join(parts)), pyfile.stem
            )
            model_map = {(f.serialization_alias or n): n for n, f in model.model_fields.items()}
            meta = cat.class_meta(pyfile.stem)
        except (ImportError, AttributeError, KeyError):
            continue
        for wire, python_name in model_map.items():
            catalogue_name = meta.wire_to_readable.get(wire)
            if catalogue_name is not None and catalogue_name != python_name:
                divergences.add((pyfile.stem, wire))
    assert divergences == _KNOWN_NAMING_DIVERGENCES


# ── R2/R3: description, discovery, search ─────────────────────────────────────


@needs_corpus
def test_describe_returns_props_faults_and_enum_values(cat: _catalog.Catalog) -> None:
    doc = cat.describe("fvBD")
    assert doc.label == "Bridge Domain"
    assert not doc.is_abstract
    readables = {p.readable for p in doc.props}
    assert "arp_flooding" in readables
    assert doc.faults.get("F2409") == "fltFvBDInvalidConfigOnBD"
    # At least one property is an enum with its members resolved.
    assert any(p.enum_values for p in doc.props)


@needs_corpus
def test_describe_abstract_lists_concrete_subclasses(cat: _catalog.Catalog) -> None:
    doc = cat.describe("fvEPg")
    assert doc.is_abstract
    assert doc.concrete_subclasses  # non-empty
    assert "fvAEPg" in doc.concrete_subclasses


@needs_corpus
def test_describe_exposes_is_observable(cat: _catalog.Catalog) -> None:
    assert cat.describe("fvBD").is_observable is True
    # The falsified-heuristic case: not observable per schema, yet live-confirmed
    # subscribable and delivering real push notifications (see StatsClassNotSubscribableError).
    assert cat.describe("faultInst").is_observable is False


@needs_corpus
def test_prop_meta_by_readable_or_wire(cat: _catalog.Catalog) -> None:
    by_readable = cat.prop_meta("fvBD", "arp_flooding")
    by_wire = cat.prop_meta("fvBD", "arpFlood")
    assert by_readable == by_wire
    assert by_readable.kind == "bool"


@needs_corpus
def test_fault_name(cat: _catalog.Catalog) -> None:
    assert cat.fault_name("F2409") == "fltFvBDInvalidConfigOnBD"
    assert cat.fault_name("F-nonexistent") is None


@needs_corpus
def test_search_finds_classes_by_label(cat: _catalog.Catalog) -> None:
    assert "fvBD" in cat.search("bridge")


@needs_corpus
def test_find_prop_locates_properties(cat: _catalog.Catalog) -> None:
    hits = cat.find_prop("arpFlood")
    assert ("fvBD", "arpFlood") in hits


@needs_corpus
def test_like_fallback_when_fts_is_unavailable(cat: _catalog.Catalog) -> None:
    cat._fts = False  # simulate a runtime whose sqlite lacks FTS5
    try:
        assert "fvBD" in cat.search("bridge")  # LIKE path still works
    finally:
        cat._fts = None


@needs_corpus
def test_names_are_unique_and_deterministic(cat: _catalog.Catalog) -> None:
    """The invariant the runtime actually needs: names are a bijection, stable.

    Full cross-model parity is *not* guaranteed: the catalogue resolves names over
    a class's whole readable property set, while a generated model resolves over
    the smaller configurable subset, so collision resolution can differ (e.g.
    ``l3extOut.enforceRtctrl`` → ``enforce_route_control`` here vs ``enforce_rtctrl``
    on the model).  That is invisible at runtime — a generated class is served by
    its model, never by the catalogue — so what matters is that the catalogue's
    own names are unique (no two wire props collide) and deterministic.
    """
    for cls in ("faultInst", "topSystem", "l3extOut", "eqptcapacityL3TotalUsage5min"):
        meta = cat.class_meta(cls)
        # Bijective: readable ↔ wire, no two properties share a readable name.
        assert len(meta.readable_to_wire) == len(meta.wire_to_readable) == len(meta.wire_to_kind)
        # Deterministic: a fresh build yields the same mapping.
        cat._meta.pop(cls, None)
        assert cat.class_meta(cls).wire_to_readable == meta.wire_to_readable
