"""Live validation of ``push(verify_refs=True)`` — the M6 matrix.

Run:
    uv run pytest tests/integration/test_verify_refs_live.py -m integration -s

Owns tenant ``nw-verify`` and vlan pool ``nw-verify-pool`` — wiped at START
only (the state stays on the simulator, per the house rule).

Measured reality this suite pins: on the 6.0(9c) simulator a dangling
static path is ACCEPTED with the relation left ``state=unformed`` and **no
fault raised at all** — the exact silent window ``verify_refs`` closes.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from niwaki import Niwaki, exceptions
from niwaki.design import design, tenant

pytestmark = pytest.mark.integration

TENANT = "nw-verify"
POOL = "nw-verify-pool"
POOL_DN = f"uni/infra/vlanns-[{POOL}]-static"
BOGUS_PATH = "topology/pod-1/paths-999/pathep-[eth1/99]"


@pytest.fixture(scope="module", autouse=True)
def _wipe_own(live_aci: Niwaki) -> None:
    """Wipe owned objects at start — never at the end."""
    for dn in (
        f"uni/tn-{TENANT}",
        "uni/tn-nw-verify-smoke",
        f"uni/phys-{TENANT}-phys",
        f"uni/phys-{TENANT}-phys2",
        POOL_DN,
    ):
        with contextlib.suppress(exceptions.APIError):
            live_aci.node(dn).delete()


def test_101_existing_target_verifies_and_forms(live_aci: Niwaki) -> None:
    """Scenario 1: a real target passes verification and the relation forms."""
    from niwaki.design import infra

    pool = infra().vlan_pool(POOL, allocation_mode="static")
    pool.push(live_aci)

    dom = design().phys_dom(f"{TENANT}-phys")
    dom.bind_dn(vlan_pool=POOL_DN)
    report = dom.push(live_aci, verify_refs=True)
    assert report.mode == "strict"

    rs = live_aci.query("infraRsVlanNs").under(f"uni/phys-{TENANT}-phys").one()
    assert rs["state"] == "formed"


def test_102_missing_target_caught_before_the_wire(live_aci: Niwaki) -> None:
    """Scenario 2a: the bogus path fails verification — nothing pushed."""
    cfg = tenant(TENANT)
    cfg.app("app").epg("web").static_path(BOGUS_PATH, encap="vlan-3999")

    with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
        cfg.push(live_aci, verify_refs=True)

    (failure,) = excinfo.value.failures
    assert failure.status == "missing"
    assert failure.ref.dn == BOGUS_PATH
    # nothing was pushed — the tenant does not exist
    assert live_aci.query("fvTenant").where(name=TENANT).first() is None


def test_103_without_the_flag_the_apic_accepts_the_dangling_config(
    live_aci: Niwaki,
) -> None:
    """Scenario 2b (control): the silent window verify_refs closes.

    The same design pushed WITHOUT the flag is accepted; the relation stays
    ``unformed`` — and on the 6.0(9c) simulator no fault is ever raised
    (measured: 30 s patience), so the unformed state is the only trace.
    """
    cfg = tenant(TENANT)
    cfg.app("app").epg("web").static_path(BOGUS_PATH, encap="vlan-3999")
    cfg.push(live_aci)  # accepted!

    rs = live_aci.query("fvRsPathAtt").under(f"uni/tn-{TENANT}").one()
    assert rs["state"] == "unformed"

    time.sleep(5)
    faults = list(live_aci.query("faultInst").under(f"uni/tn-{TENANT}"))
    # Simulator reality: the dangling path raises nothing. If a future
    # firmware starts faulting here, this assert documents the change.
    assert faults == []


def test_104_wrong_class_target_is_rejected(live_aci: Niwaki) -> None:
    """Scenario 3: an existing DN of the wrong class fails verification."""
    dom = design().phys_dom(f"{TENANT}-phys2")
    dom.bind_dn(vlan_pool="uni/tn-common")  # exists — but it is a tenant

    with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
        dom.push(live_aci, verify_refs=True)

    (failure,) = excinfo.value.failures
    assert failure.status == "wrong_class"
    assert failure.found == "fvTenant"
    assert "fvnsVlanInstP" in failure.expected


def test_105_real_fabric_path_verifies(live_aci: Niwaki) -> None:
    """Scenario 4: a discovered fabric path endpoint passes the class check.

    fabricPathEp is read-only (outside the generated set) — the exact class
    the catalogue accept-set must honor.
    """
    path = live_aci.query("fabricPathEp").where(name="eth1/1").first()
    if path is None:
        pytest.skip("no path endpoints discovered — the simulator models no hardware")

    cfg = tenant(TENANT)
    cfg.app("app").epg("real").static_path(path.dn, encap="vlan-3901")
    plan = cfg.push(live_aci, mode="plan", verify_refs=True)

    by_dn = {c.ref.dn: c.status for c in plan.external_refs}
    assert by_dn[path.dn] == "ok"


def test_106_scale_probe_read_amplification(live_aci: Niwaki) -> None:
    """Scenario 5: the lot-4 gate — measure the per-DN read cost at scale.

    The simulator discovers no fabricPathEp, so the probe verifies 24
    bind_dn references against 24 real vlan pools instead — same read
    pattern (one GET per unique external DN).
    """
    from niwaki.design import infra

    pools = infra()
    for i in range(24):
        pools.vlan_pool(f"{POOL}-sp{i}", allocation_mode="static")
    pools.push(live_aci)

    d = design()
    for i in range(24):
        d.phys_dom(f"{TENANT}-sp{i}").bind_dn(vlan_pool=f"uni/infra/vlanns-[{POOL}-sp{i}]-static")

    start = time.monotonic()
    plan = d.push(live_aci, mode="plan", verify_refs=True)
    elapsed = time.monotonic() - start

    assert len(plan.external_refs) == 24
    assert all(c.status == "ok" for c in plan.external_refs)
    print(f"\n  scale probe: 24 unique DNs verified in {elapsed:.2f}s")
    assert elapsed < 30  # generous bound — batching (lot 4) only if this hurts
