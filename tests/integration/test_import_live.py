"""Gate A (live) — the reverse import reproduces the fabric, provably.

Run:
    uv run pytest tests/integration/test_import_live.py -m integration -s

2.0 it.2 lot B exit gate, live half.  The offline half
(``tests/design/test_import_gate_a.py``) proves ``design → payload →
pseudo-snapshot → to_design → payload`` byte-equal on the golden worlds;
this file closes the loop against a real controller:

    snapshot.take(aci, "uni")  →  to_design(snap)  →  push(mode="plan")

must report **zero changes**: the imported design *is* the fabric.  A
create in the plan means the importer dropped or misplaced an object; a
change means a value did not survive the wire→typed→wire trip.

Read-only: ``take`` reads, ``to_design`` is pure, and ``mode="plan"``
never writes — safe at any point of the suite, no ``wipe()``.
"""

from __future__ import annotations

from typing import Any

import pytest

from niwaki import Niwaki, snapshot
from niwaki.design import to_design

pytestmark = pytest.mark.integration


def test_uni_snapshot_imports_and_plans_clean(live_aci: Niwaki) -> None:
    """take(uni) → to_design → plan: the design and the fabric must agree."""
    snap = snapshot.take(live_aci, "uni")
    assert snap.tree is not None, "the sim answered an empty uni scope"

    # redacted="skip" is the canonical real-fabric flow: every live fabric
    # carries redacted admin secrets (otpkey, jwtApiKey…) the capture elides.
    cfg = to_design(snap, redacted="skip")

    result = cfg.push(live_aci, mode="plan")
    noisy = [*result.creates[:5], *list(result.updates.items())[:5]]
    assert not result.has_changes, (
        f"the imported design diverges from the fabric it was taken from: "
        f"{len(result.creates)} create(s), {len(result.updates)} update(s) — "
        f"first: {noisy}"
    )


def test_import_is_deterministic_across_two_takes(live_aci: Niwaki) -> None:
    """Two takes of an unchanged fabric import to the same payload bytes."""
    import json

    first = to_design(snapshot.take(live_aci, "uni"), redacted="skip").to_payload()
    second = to_design(snapshot.take(live_aci, "uni"), redacted="skip").to_payload()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_scoped_import_and_slice_plan_clean(live_aci: Niwaki) -> None:
    """it.3 live gate: the scoped door and the carve door agree with the fabric.

    Both compositions must plan clean against the fabric they came from:
    a tenant captured scoped (``take(uni/tn-X)``) and the same tenant carved
    out of a full-fabric import (``slice``).
    """
    full = to_design(snapshot.take(live_aci, "uni"), redacted="skip")
    tenants = [n for n in full.view().by_class("fvTenant")]
    assert tenants, "the sim carries no tenant"
    dn = tenants[0].dn

    carved = full.slice(dn)
    result = carved.push(live_aci, mode="plan")
    assert not result.has_changes, (
        f"sliced {dn} diverges: {result.creates[:3]} {list(result.updates.items())[:3]}"
    )

    scoped = to_design(snapshot.take(live_aci, dn), redacted="skip")
    result2 = scoped.push(live_aci, mode="plan")
    assert not result2.has_changes, (
        f"scoped {dn} diverges: {result2.creates[:3]} {list(result2.updates.items())[:3]}"
    )


def test_emitted_code_replays_the_fabric(live_aci: Niwaki) -> None:
    """it.4 live gate, emitter half: to_code(to_design(fabric)) replays clean.

    The full circle on a real controller: capture → import → emit source →
    execute the source → the replayed design plans zero changes against the
    fabric it came from.
    """
    from niwaki.design import to_code

    cfg = to_design(snapshot.take(live_aci, "uni"), redacted="skip")
    namespace: dict[str, Any] = {}
    exec(to_code(cfg), namespace)
    replayed = namespace["cfg"]
    result = replayed.push(live_aci, mode="plan")
    assert not result.has_changes, (
        f"replayed source diverges: {result.creates[:3]} {list(result.updates.items())[:3]}"
    )


def test_reconcile_is_clean_for_a_full_import(live_aci: Niwaki) -> None:
    """it.4 live gate, reconciliation half: an imported design owns its fabric."""
    from niwaki.design import reconcile

    cfg = to_design(snapshot.take(live_aci, "uni"), redacted="skip")
    report = reconcile(cfg, live_aci)
    assert report.clean, f"unexpected extras: {report.orphan_subtrees[:5]}"
