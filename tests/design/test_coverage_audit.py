"""Drift guard for the curation-coverage audit.

The audit (:mod:`niwaki._codegen.coverage_audit`) enumerates every curated
parent whose child stays unreachable from the DSL.  This test pins that set to a
committed snapshot so the backlog can only move deliberately:

- **a new gap appears** (codegen adds a class, or a new parent is curated) →
  the snapshot mismatches → a human must triage the class (give it a scope
  reason, or curate it) and refresh the snapshot;
- **a gap disappears** (it gets curated, or a class is renamed) → the snapshot
  mismatches → refresh it, shrinking the backlog on the record.

The direction is safe by construction: an unrecorded gap fails the build rather
than slipping through, so coverage never silently regresses.

Refresh after an intended change::

    uv run python -m niwaki._codegen.coverage_audit --json > tests/design/coverage_gaps.json
"""

from __future__ import annotations

import json
from pathlib import Path

from niwaki._codegen.coverage_audit import SCOPE_RULES, classify, scan_gaps, snapshot

_SNAPSHOT = Path(__file__).parent / "coverage_gaps.json"


def _committed() -> list[str]:
    return json.loads(_SNAPSHOT.read_text())


def test_gap_snapshot_is_current() -> None:
    """The live scan matches the committed snapshot — no undocumented drift."""
    live = set(snapshot())
    pinned = set(_committed())
    added = sorted(live - pinned)
    removed = sorted(pinned - live)
    assert not added and not removed, (
        "Curation coverage drifted from the snapshot.\n"
        f"  NEW, untriaged gaps ({len(added)}): {added[:10]}\n"
        f"  gaps that disappeared ({len(removed)}): {removed[:10]}\n"
        "Triage the new classes (scope reason or curate them), then refresh:\n"
        "  uv run python -m niwaki._codegen.coverage_audit --json "
        "> tests/design/coverage_gaps.json"
    )


def test_snapshot_keys_are_well_formed() -> None:
    """Every snapshot key is ``kind:child@parent`` with a known kind."""
    for key in _committed():
        kind, _, rest = key.partition(":")
        child, sep, parent = rest.partition("@")
        assert kind in {"maker", "bind"}, key
        assert sep == "@" and child and parent, key


def test_every_scope_rule_matches_a_real_gap() -> None:
    """No dead exclusion: each scope rule covers at least one detected gap.

    A rule that matches nothing is a stale judgement (the class was renamed or
    already curated) and should be removed, not left to rot.
    """
    children = {g.child for g in scan_gaps()}
    for rule in SCOPE_RULES:
        assert any(rule.matches(c) for c in children), (
            f"scope rule matches no current gap: {rule.reason!r} "
            f"(pkg={rule.pkg}, pattern={rule.pattern}, cls={rule.cls})"
        )


def test_classification_partitions_every_gap() -> None:
    """Each gap classifies into exactly one bucket; buckets sum to the whole."""
    gaps = scan_gaps()
    buckets = {"in": 0, "deferred": 0, "out": 0}
    for g in gaps:
        bucket, reason = classify(g.child)
        assert bucket in buckets, g.child
        assert reason, g.child
        buckets[bucket] += 1
    assert sum(buckets.values()) == len(gaps)
