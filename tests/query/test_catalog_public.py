"""The public discovery surface, ``niwaki.catalog`` — a thin door to the reader.

These assert the public functions delegate to the lazily-opened catalogue and
re-export its result types; the reader itself is tested in ``test_catalog.py``.
They need the catalogue ``.db`` built from the raw schemas, so they skip when the
corpus is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from niwaki import catalog
from niwaki._codegen import generate_catalog as gc
from niwaki.query import _catalog

CORPUS_PRESENT = gc.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


def test_public_types_are_the_readers_types() -> None:
    assert catalog.ClassDoc is _catalog.ClassDoc
    assert catalog.PropDoc is _catalog.PropDoc
    assert catalog.ClassMeta is _catalog.ClassMeta


@pytest.fixture(scope="module")
def catalog_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("cat") / "catalog.db"
    gc.build_catalog(out=out)
    return out


@pytest.fixture
def wired(catalog_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_catalog, "_instance", _catalog.Catalog(catalog_path))


@needs_corpus
def test_describe(wired: None) -> None:
    doc = catalog.describe("fvBD")
    assert doc.label == "Bridge Domain"
    assert any(p.readable == "arp_flooding" for p in doc.props)


@needs_corpus
def test_prop_meta(wired: None) -> None:
    assert catalog.prop_meta("fvBD", "arp_flooding").kind == "bool"


@needs_corpus
def test_search(wired: None) -> None:
    assert "fvBD" in catalog.search("bridge")


@needs_corpus
def test_find_prop(wired: None) -> None:
    assert ("fvBD", "arpFlood") in catalog.find_prop("arpFlood")


@needs_corpus
def test_concrete_subclasses(wired: None) -> None:
    assert "fvAEPg" in catalog.concrete_subclasses("fvEPg")


@needs_corpus
def test_class_meta(wired: None) -> None:
    meta = catalog.class_meta("fvBD")
    assert meta.readable_to_wire["arp_flooding"] == "arpFlood"


@needs_corpus
def test_fault_name(wired: None) -> None:
    assert catalog.fault_name("F2409") == "fltFvBDInvalidConfigOnBD"


@needs_corpus
def test_fault_name_unknown_code(wired: None) -> None:
    assert catalog.fault_name("F-nonexistent") is None
