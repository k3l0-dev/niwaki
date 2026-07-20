"""Tests for the read-catalogue generator.

The unit tests (pools, enum store, flag packing, coverage) run everywhere.  The
lossless-reconstruction and size proofs need the raw APIC schemas
(``data/schemas/``, gitignored) and are skipped when the corpus is absent — the
same gate the design coverage audit uses.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from niwaki._codegen import generate_catalog as gc

CORPUS_PRESENT = gc.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


# ── Unit: interning and encoding (no corpus) ──────────────────────────────────


def test_pool_interns_identical_text_once() -> None:
    pool = gc._Pool()
    assert pool.intern("alpha") == 0
    assert pool.intern("beta") == 1
    assert pool.intern("alpha") == 0  # same text → same id
    assert pool.rows() == [(0, "alpha"), (1, "beta")]


def test_enum_store_dedups_by_content() -> None:
    store = gc._EnumStore()
    first = store.add([{"localName": "a"}, {"localName": "b"}])
    same = store.add([{"localName": "a"}, {"localName": "b"}])
    other = store.add([{"localName": "c"}])
    assert first == same != other
    assert len(store.rows()) == 2


def _full_prop(**overrides: Any) -> dict[str, Any]:
    """A property carrying all 14 flags (as the corpus always does)."""
    prop: dict[str, Any] = {flag: False for flag in gc.PROP_FLAG_ORDER}
    prop.update(overrides)
    return prop


def test_pack_flags_roundtrips_every_bit() -> None:
    prop = _full_prop(isNaming=True, readOnly=True, isHidden=True)
    bits = gc._pack_flags(prop, "fvBD", "name")
    for i, flag in enumerate(gc.PROP_FLAG_ORDER):
        assert bool(bits & (1 << i)) is prop[flag]


def test_pack_flags_rejects_a_missing_flag() -> None:
    prop = _full_prop()
    del prop["isNaming"]
    with pytest.raises(gc.UnroutedSchemaKeyError, match="isNaming"):
        gc._pack_flags(prop, "fvBD", "name")


def test_kind_value_or_none_classifies_or_reports() -> None:
    from niwaki._codegen.basetypes import kind_value_or_none

    assert kind_value_or_none("scalar:Bool") == "bool"
    assert kind_value_or_none("scalar:Uint32") == "int"
    # A read-only family beyond the configurable model set → None, not a guess.
    assert kind_value_or_none("reference:BinRN") is None
    assert kind_value_or_none("") is None


# ── Unit: fail-loud coverage on synthetic schemas ─────────────────────────────


def _write_schema(directory: Path, wire_colon: str, cls: dict[str, Any]) -> None:
    name = wire_colon.replace(":", "")
    (directory / f"{name}.json").write_text(json.dumps({wire_colon: cls}))


def test_build_rejects_an_unrouted_class_key(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    _write_schema(
        schemas,
        "x:Foo",
        {"classPkg": "x", "className": "Foo", "readAccess": ["admin"], "newFangled": 1},
    )
    with pytest.raises(gc.UnroutedSchemaKeyError, match="newFangled"):
        gc.build_catalog(schemas, tmp_path / "c.db")


def test_build_rejects_an_unrouted_property_key(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    prop = _full_prop(baseType="scalar:Bool", surpriseKey=1)
    _write_schema(
        schemas,
        "x:Foo",
        {
            "classPkg": "x",
            "className": "Foo",
            "readAccess": ["admin"],
            "properties": {"p": prop},
        },
    )
    with pytest.raises(gc.UnroutedSchemaKeyError, match="surpriseKey"):
        gc.build_catalog(schemas, tmp_path / "c.db")


# ── Corpus: the real build, its size, and lossless reconstruction ─────────────


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, int]]:
    out = tmp_path_factory.mktemp("catalog") / "catalog.db"
    stats = gc.build_catalog(out=out)
    return out, stats


@needs_corpus
def test_covers_every_readable_class(built: tuple[Path, dict[str, int]]) -> None:
    _, stats = built
    assert stats["classes"] == 15301
    assert stats["properties"] > 300_000


@needs_corpus
def test_ships_under_the_wheel_budget(built: tuple[Path, dict[str, int]]) -> None:
    _, stats = built
    # Lot 0 measured the naive verbatim capture at 87 MB (over PyPI's ceiling).
    # The derived tables + relation/scopemeta + the FTS index stay ~31 MB, well
    # under PyPI's 100 MB per-file limit.
    assert stats["size_bytes"] < 32 * 1024 * 1024


def _load_original(name: str) -> dict[str, Any]:
    doc = json.loads((gc.SCHEMA_DIR / f"{name}.json").read_bytes())
    return next(iter(doc.values()))


def _expected(cls: dict[str, Any]) -> dict[str, Any]:
    """The source schema minus what the catalogue drops, by decision."""
    exp = {k: v for k, v in cls.items() if k not in gc.CLASS_DROPPED}
    props: dict[str, Any] = {}
    for wire, prop in (cls.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue  # the build skips non-dict properties too
        kept = {k: v for k, v in prop.items() if k not in gc.PROP_DROPPED}
        if str(prop.get("baseType", "")).startswith("mo:"):
            kept.pop("validValues", None)  # identifier registers are dropped
        props[wire] = kept
    exp["properties"] = props
    return exp


@needs_corpus
def test_reconstruction_is_lossless(built: tuple[Path, dict[str, int]]) -> None:
    out, _ = built
    con = sqlite3.connect(out)
    try:
        names = [n for (n,) in con.execute("SELECT class_name FROM mo ORDER BY id")]
        # A representative set (concrete, abstract, stats, faults, hardware) plus a
        # stride across every package — a broad slice, cheap enough for the suite.
        sample = {
            n
            for n in ("fvBD", "fvCEp", "fvAEPg", "fvEPg", "faultInst", "topSystem")
            if n in set(names)
        }
        sample |= set(names[::37])
        for name in sorted(sample):
            assert gc.reconstruct_class(con, name) == _expected(_load_original(name)), (
                f"reconstruction diverged for {name}"
            )
    finally:
        con.close()


@needs_corpus
def test_point_lookup_and_pools_resolve(built: tuple[Path, dict[str, int]]) -> None:
    out, _ = built
    con = sqlite3.connect(out)
    try:
        rebuilt = gc.reconstruct_class(con, "fvBD")
        assert rebuilt["classPkg"] == "fv"
        assert "arpFlood" in rebuilt["properties"]
        arp = rebuilt["properties"]["arpFlood"]
        assert arp["baseType"] == "scalar:Bool"  # resolved through the type pool
        assert arp["label"] == "ARP Flooding"  # resolved through the label pool
        assert isinstance(rebuilt["faults"], dict)
    finally:
        con.close()


@needs_corpus
def test_fts_and_like_fallback_agree_on_a_term(built: tuple[Path, dict[str, int]]) -> None:
    out, _ = built
    con = sqlite3.connect(out)
    try:
        fts = {
            n
            for (n,) in con.execute(
                "SELECT m.class_name FROM catalog_fts f JOIN mo m ON m.id = f.rowid "
                "WHERE catalog_fts MATCH 'endpoint'"
            )
        }
        like = {
            n
            for (n,) in con.execute(
                "SELECT m.class_name FROM search_doc s JOIN mo m ON m.id = s.class_id "
                "WHERE s.text LIKE '%endpoint%'"
            )
        }
        assert fts, "FTS returned nothing for a common term"
        # The LIKE fallback is a superset of FTS: FTS matches the token
        # "endpoint", LIKE matches the substring (so it also catches it inside
        # camelCase names like hcloudEndPointOper). Broader, less precise, correct.
        assert fts <= like
    finally:
        con.close()


@needs_corpus
def test_relation_table_from_relation_info(built: tuple[Path, dict[str, int]]) -> None:
    out, _ = built
    con = sqlite3.connect(out)
    try:
        row = con.execute(
            "SELECT to_mo, cardinality FROM relation WHERE rs_class = 'fvRsCtx'"
        ).fetchone()
        assert row == ("fv:Ctx", "n-to-1")  # fvRsCtx → the VRF it binds
    finally:
        con.close()


@needs_corpus
def test_scopemeta_stored_for_naming_parity(built: tuple[Path, dict[str, int]]) -> None:
    out, _ = built
    con = sqlite3.connect(out)
    try:
        # A scopemeta class the runtime will need to name exactly like its model.
        labels = dict(
            con.execute(
                "SELECT s.wire_name, s.sm_label FROM scopemeta s "
                "JOIN mo m ON m.id = s.class_id WHERE m.class_name = 'aaaAuthRealm'"
            )
        )
        assert labels.get("defRolePolicy") == "role-policy-for-error-remote-authentication"
    finally:
        con.close()


def _content_hash(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        (value,) = con.execute("SELECT value FROM manifest WHERE key='content_hash'").fetchone()
        return str(value)
    finally:
        con.close()


@needs_corpus
def test_shipped_db_is_fresh(tmp_path: Path) -> None:
    """The committed ``catalog.db`` must match a rebuild from the current schemas.

    The freshness guard: it catches a stale shipped artifact after a generator
    change or an APIC firmware bump.  Corpus-gated, so the public CI (no schemas)
    skips it while a dev checkout enforces it.
    """
    shipped = gc.DEFAULT_OUT
    if not shipped.exists():
        pytest.skip("catalog.db is not present in this checkout")
    rebuilt = tmp_path / "rebuilt.db"
    gc.build_catalog(out=rebuilt)
    assert _content_hash(shipped) == _content_hash(rebuilt), (
        "src/niwaki/query/_catalog/catalog.db is stale — regenerate it with "
        "'uv run python -m niwaki._codegen.generate_catalog' and commit the result."
    )


@needs_corpus
def test_manifest_records_provenance(built: tuple[Path, dict[str, int]]) -> None:
    out, stats = built
    con = sqlite3.connect(out)
    try:
        manifest = dict(con.execute("SELECT key, value FROM manifest").fetchall())
        assert manifest["apic_version"] == gc.APIC_VERSION
        assert len(manifest["content_hash"]) == 40
        assert int(manifest["classes"]) == stats["classes"]
    finally:
        con.close()
