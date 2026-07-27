"""Freshness guard — the committed _child_map.py matches a fresh regeneration.

The catalogue has had this guard since 1.2.0 (test_shipped_db_is_fresh); the
navigation tables did not — a generator change without a regen would ship
stale tables silently. Corpus-gated, format-independent: the comparison is on
the built dicts, not the rendered text.
"""

from __future__ import annotations

import pytest
import yaml

from niwaki._codegen import generate_domain as gd
from niwaki.domain import _child_map as committed

CORPUS_PRESENT = gd.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


@needs_corpus
def test_committed_navigation_tables_match_regeneration() -> None:
    classes = gd._load_schemas()
    vocabulary = yaml.safe_load(gd.VOCABULARY_FILE.read_text())

    child_map, _ = gd._build_child_map(classes, vocabulary.get("jargon", {}))
    gd._apply_maker_overlay(child_map, vocabulary.get("makers", {}))
    nav_deprecated, shadowed = gd._build_nav_deprecated(child_map)

    assert not shadowed
    hint = "regenerate: uv run python -m niwaki._codegen.generate_domain"
    assert {p: row for p, row in child_map.items() if row} == committed.CHILD_MAP, hint
    assert nav_deprecated == committed.NAV_DEPRECATED, hint
    assert gd._build_rs_target_prop(classes) == committed.RS_TARGET_PROP, hint

    reference_map, target_subclasses, _stats = gd._build_reference_map(classes)
    assert reference_map == committed.REFERENCE_MAP, hint
    assert target_subclasses == committed.TARGET_SUBCLASSES, hint
    assert {c: i["classPkg"] for c, i in classes.items()} == committed.CLASS_PKG, hint
