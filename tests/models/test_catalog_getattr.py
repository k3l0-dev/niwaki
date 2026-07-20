"""Catalogue-backed readable attribute access on results (Lot 3).

``_coerce_read`` is pure and tested without the corpus.  The ``__getattr__``
integration builds non-generated objects via ``from_apic`` and resolves their
readable names through a catalogue built from the raw schemas, so those tests
skip when the corpus is absent.
"""

from __future__ import annotations

import pytest

from niwaki._codegen import generate_catalog as gc
from niwaki.models.base import ManagedObject, _coerce_read
from niwaki.query import _catalog

CORPUS_PRESENT = gc.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


# ── _coerce_read: pure, exhaustive (no corpus) ────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("bool", "yes", True),
        ("bool", "true", True),
        ("bool", "1", True),  # the wire boundary's full truthy set, not just yes/true
        ("bool", "on", True),
        ("bool", "no", False),
        ("bool", "false", False),
        ("bool", "0", False),
        ("bool", "off", False),
        ("bool", "not-applicable", "not-applicable"),  # sentinel → raw, never a bogus False
        ("int", "100", 100),
        ("int", "-5", -5),
        ("int", "0xff", 255),  # hex, like the wire boundary
        ("int", "not-a-number", "not-a-number"),  # APIC trusted: no exception
        ("float", "1.5", 1.5),
        ("float", "nan-ish", "nan-ish"),
        ("flags", "public,shared", frozenset({"public", "shared"})),
        ("flags", "public", frozenset({"public"})),
        ("flags", "", frozenset()),
        ("mac", "00:11:22:33:44:55", "00:11:22:33:44:55"),
        ("ip", "10.0.0.5/32", "10.0.0.5/32"),
        ("enum", "active", "active"),
        ("named_number", "http", "http"),
        ("str", "anything", "anything"),
        (None, "raw", "raw"),  # unknown kind → untouched
        ("bool", None, None),  # non-string → untouched
        ("int", 5, 5),  # already coerced → untouched
    ],
)
def test_coerce_read(kind: str | None, value: object, expected: object) -> None:
    assert _coerce_read(value, kind) == expected


# ── __getattr__ over a real catalogue ─────────────────────────────────────────


@pytest.fixture(scope="module")
def catalog_path(tmp_path_factory: pytest.TempPathFactory) -> object:
    out = tmp_path_factory.mktemp("cat") / "catalog.db"
    gc.build_catalog(out=out)
    return out


@pytest.fixture
def catalog_db(catalog_path: object, monkeypatch: pytest.MonkeyPatch) -> _catalog.Catalog:
    inst = _catalog.Catalog(catalog_path)  # type: ignore[arg-type]
    monkeypatch.setattr(_catalog, "_instance", inst)  # the process-wide reader
    return inst


def _read(wire_class: str, attrs: dict[str, str]) -> ManagedObject:
    return ManagedObject.from_apic({wire_class: {"attributes": attrs}})


# ``topSystem`` is operational (not configurable) → never a generated model, so
# ``from_apic`` returns the base class and the catalogue path is exercised for
# real, in isolation *and* in the full suite.  Its ``address`` reads ``infrastructure_ip``.
_OP_CLASS = "topSystem"
_OP_WIRE = "address"


@needs_corpus
def test_nongenerated_object_is_the_base_class(catalog_db: _catalog.Catalog) -> None:
    top = _read(_OP_CLASS, {_OP_WIRE: "10.0.0.1"})
    assert type(top) is ManagedObject  # topSystem is operational, never generated


@needs_corpus
def test_readable_access_on_a_nongenerated_object(catalog_db: _catalog.Catalog) -> None:
    readable = catalog_db.class_meta(_OP_CLASS).wire_to_readable[_OP_WIRE]
    assert readable != _OP_WIRE  # it is renamed → the catalogue path really runs
    top = _read(_OP_CLASS, {_OP_WIRE: "10.0.0.1"})
    assert getattr(top, readable) == "10.0.0.1"


@needs_corpus
def test_wire_access_is_unchanged(catalog_db: _catalog.Catalog) -> None:
    top = _read(_OP_CLASS, {_OP_WIRE: "10.0.0.1"})
    assert top[_OP_WIRE] == "10.0.0.1"  # bare wire item access (etage 0)


@needs_corpus
def test_coercion_on_a_renamed_property(catalog_db: _catalog.Catalog) -> None:
    """A renamed int property reads coerced via its readable name; the wire name is raw.

    Coercion via attribute happens for *renamed* properties (readable ≠ wire): a
    property spelled like its wire name is resolved by Pydantic's extras as a raw
    string, before ``__getattr__`` runs.  Searches non-generated classes for a
    renamed int property so the coercion path actually executes.
    """
    for cls in ("topSystem", "eqptCh", "fvRsCEpToPathEp", "eqptcapacityL3TotalUsage5min"):
        meta = catalog_db.class_meta(cls)
        wire = next(
            (
                w
                for w, k in meta.wire_to_kind.items()
                if k == "int" and meta.wire_to_readable[w] != w
            ),
            None,
        )
        if wire is None:
            continue
        obj = _read(cls, {wire: "42"})
        assert getattr(obj, meta.wire_to_readable[wire]) == 42  # readable → coerced int
        assert getattr(obj, wire) == "42"  # wire → raw extra string
        return
    pytest.skip("no renamed int property found in the sample classes")


@needs_corpus
def test_homonym_property_is_coerced_too(catalog_db: _catalog.Catalog) -> None:
    """Uniform coercion: a property whose readable name == wire name coerces too.

    Previously served raw by Pydantic's extras (only renamed props coerced); the
    catalogue is now consulted first, so every known property is the typed view,
    while item access ``obj[wire]`` stays the raw wire string.
    """
    for cls in ("topSystem", "eqptCh", "fvRsCEpToPathEp", "faultInst"):
        meta = catalog_db.class_meta(cls)
        wire = next(
            (
                w
                for w, k in meta.wire_to_kind.items()
                if k in ("bool", "int") and meta.wire_to_readable[w] == w
            ),
            None,
        )
        if wire is None:
            continue
        is_int = meta.wire_to_kind[wire] == "int"
        obj = _read(cls, {wire: "42" if is_int else "yes"})
        assert getattr(obj, wire) == (42 if is_int else True)  # homonym → coerced
        assert obj[wire] == ("42" if is_int else "yes")  # item access stays raw
        return
    pytest.skip("no homonym bool/int property found in the sample classes")


@needs_corpus
def test_generated_field_wins_and_skips_the_catalogue(catalog_db: _catalog.Catalog) -> None:
    from niwaki.models._generated.fv.fvBD import fvBD

    bd = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}})
    # A typed field resolves before __getattr__ ever runs — and is coerced.
    assert bd.arp_flooding is True


@needs_corpus
def test_unknown_readable_name_raises(catalog_db: _catalog.Catalog) -> None:
    top = _read(_OP_CLASS, {_OP_WIRE: "10.0.0.1"})
    with pytest.raises(AttributeError):
        _ = top.definitely_not_a_property


@needs_corpus
def test_readable_name_present_but_value_absent_raises(catalog_db: _catalog.Catalog) -> None:
    readable = catalog_db.class_meta(_OP_CLASS).wire_to_readable[_OP_WIRE]
    top = _read(_OP_CLASS, {"name": "leaf-101"})  # address not returned by the APIC
    with pytest.raises(AttributeError):
        _ = getattr(top, readable)


def test_locally_built_object_has_no_catalogue_access() -> None:
    obj = ManagedObject.model_construct()  # no _wire_class → no catalogue
    with pytest.raises(AttributeError):
        _ = obj.anything_readable


def test_private_and_dunder_names_never_hit_the_catalogue() -> None:
    obj = ManagedObject.model_construct()
    with pytest.raises(AttributeError):
        _ = obj._not_a_field
    with pytest.raises(AttributeError):
        _ = obj.__nonexistent_dunder__


@needs_corpus
def test_missing_catalogue_degrades_to_attributeerror(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = _catalog.Catalog(tmp_path_factory.mktemp("x") / "absent.db")
    monkeypatch.setattr(_catalog, "_instance", absent)
    top = _read(_OP_CLASS, {_OP_WIRE: "10.0.0.1"})
    with pytest.raises(AttributeError):  # FileNotFoundError must not leak out
        _ = top.infrastructure_ip
