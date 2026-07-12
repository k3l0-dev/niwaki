"""Live gate for ADR-001 risk R-1 — never-creatable carriers accept upserts.

The design DSL compiles every domain into one ``polUni`` envelope whose
intermediate nodes (``infraInfra``, ``fabricInst``, ``ctrlrInst``) are
``isCreatableDeletable: never`` classes.  The APIC must treat them as
attribute-less upsert carriers when they arrive inside a strict-mode POST —
this test proves it (or fails loudly, triggering the documented fallback
decision *before* the facade write path is demolished).

Run against the lab simulator:
    uv run pytest tests/integration/test_carrier_upsert.py -m integration
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from niwaki import Niwaki
from niwaki.design import design

pytestmark = pytest.mark.integration

_SMOKE = "niwaki-smoke"


@pytest.fixture
def cleanup(live_aci: Niwaki) -> Iterator[None]:
    """Remove every smoke object, before and after the test (idempotent)."""

    def _wipe() -> None:
        for node in (
            live_aci.root.fabric().date_and_time_policy(_SMOKE),
            live_aci.root.infra().aaep(_SMOKE),
            live_aci.root.phys_dom(_SMOKE),
        ):
            with contextlib.suppress(Exception):
                node.delete()

    _wipe()
    yield
    _wipe()


def test_carriers_accept_attributeless_upsert(live_aci: Niwaki, cleanup: None) -> None:
    """One strict push crossing fabric, infra and phys-dom carriers."""
    cfg = design()
    cfg.fabric().datetime_policy(_SMOKE, description="ADR-001 R-1 smoke")
    cfg.infra().aaep(_SMOKE, description="ADR-001 R-1 smoke")
    cfg.phys_dom(_SMOKE)

    report = cfg.push(live_aci, mode="strict")
    assert report.request_count == 1

    # The carriers were traversed, not created — the declared leaves exist.
    assert live_aci.query("datetimePol").where(name=_SMOKE).count() == 1
    assert live_aci.query("infraAttEntityP").where(name=_SMOKE).count() == 1
    assert live_aci.query("physDomP").where(name=_SMOKE).count() == 1


def test_bgp_instance_default_is_upsertable(live_aci: Niwaki, cleanup: None) -> None:
    """``bgpInstPol`` rejects creation but must accept the built-in ``default``.

    Documented exception (walkthrough act 1): route reflectors live under the
    built-in ``uni/fabric/bgpInstP-default`` — the design declares it as an
    upsert, never as a new object.
    """
    cfg = design()
    cfg.fabric().bgp_instance("default")

    report = cfg.push(live_aci, mode="strict")
    assert report.request_count == 1
    assert live_aci.query("bgpInstPol").where(name="default").count() == 1
