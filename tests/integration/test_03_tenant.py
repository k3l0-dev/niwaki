"""Act 3 — tenant: the three-tier application, published on act-2's cabling.

One mental model, end to end (ADR-001): **describe with the design DSL,
apply with push, observe with the facade**.

1. **Plan → strict push → idempotence** — the app in operator vocabulary,
   dry-run first, one atomic POST, replan clean, drift pinpointed.
2. **Staged push** — the same DSL compiled to per-object waves.
3. **Day-2 declaratively** — a description patch and a drift check are just
   smaller designs: set + push, audited by plan (D-1/D-7).
4. **The bridge to the physical world** — EPG domain attach through the
   abstract ``domain`` bind (declared phys-dom) and through ``bind_dn``
   (raw DN, D-3b), then the static VPC path as a literal-DN maker.
5. **Query audit** and the final cleanup of the whole walkthrough — the
   facade observes and deletes; it never configures.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, PlanResult, PushReport, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.tag.tagTag import tagTag
from tests.integration.conftest import (
    PHYS_DOM,
    PHYS_DOM_DN,
    TENANT,
    TENANT_DEV,
    VPC_PATH_DN,
    wipe_all,
    wipe_tenant,
)

pytestmark = pytest.mark.integration


def showcase_design() -> Cursor:
    """Three-tier app in operator vocabulary — no ACI class in sight.

    Note: filter ports deliberately avoid 80/443 — the APIC normalises those
    to their named forms ("http"/"https") on read, which would defeat the
    plan-idempotence demonstration.
    """
    return (
        tenant(TENANT, description="niwaki walkthrough act 3")
        .app("shop")
            .epg("frontend").bind(bd="frontend").consume("web-api")
            .epg("backend").bind(bd="backend").provide("web-api").consume("db")
            .epg("database").bind(bd="database").provide("db")
        .bd("frontend")
            .set(unicast_routing=True, arp_flooding=True)
            .subnet("10.10.1.1/24")
            .bind(vrf="prod")
        .bd("backend")
            .set(unicast_routing=True)
            .subnet("10.10.2.1/24")
            .bind(vrf="prod")
        .bd("database")
            .set(unicast_routing=False, arp_flooding=True)  # pure L2 tier
            .bind(vrf="prod")
        .vrf("prod")
        .filter("web")
            .entry("api", tcp=8080)
        .filter("db")
            .entry("postgres", tcp=5432)
        .contract("web-api")
            .set(scope="vrf")
            .subject("api").bind(filter="web")
        .contract("db")
            .subject("sql").bind(filter="db")
    )  # fmt: skip


class Test3TenantApplication:
    """Day-N: the application, from dry run to static path binding."""

    # ── 1 · Design DSL: plan → atomic push → idempotence ────────────────────

    def test_01_plan_is_a_pure_dry_run(self, live_aci: Niwaki) -> None:
        wipe_tenant(live_aci)
        plan = showcase_design().push(live_aci, mode="plan")

        assert isinstance(plan, PlanResult)
        assert plan.has_changes
        assert f"uni/tn-{TENANT}" in plan.creates
        assert plan.updates == {}
        with pytest.raises(NotFoundError):
            live_aci.tenant(TENANT).read()

    def test_02_strict_push_is_one_atomic_post(self, live_aci: Niwaki) -> None:
        report = showcase_design().push(live_aci, mode="strict")

        assert isinstance(report, PushReport)
        assert report.request_count == 1
        assert live_aci.tenant(TENANT).bd("frontend").read().unicast_routing is True

    def test_03_replan_is_idempotent(self, live_aci: Niwaki) -> None:
        plan = showcase_design().push(live_aci, mode="plan")
        assert not plan.has_changes
        assert f"uni/tn-{TENANT}" in plan.unchanged

    def test_04_plan_pinpoints_a_drift(self, live_aci: Niwaki) -> None:
        # Simulate out-of-band drift with a minimal counter-design.
        tenant(TENANT).bd("frontend").set(unicast_routing=False).push(live_aci)

        plan = showcase_design().push(live_aci, mode="plan")
        bd_dn = f"uni/tn-{TENANT}/BD-frontend"
        assert plan.updates == {bd_dn: {"unicast_routing": (False, True)}}

        showcase_design().push(live_aci, mode="strict")  # converge back
        assert not showcase_design().push(live_aci, mode="plan").has_changes

    # ── 2 · Staged push: same DSL, one op per object ─────────────────────────

    def test_05_staged_push_runs_in_waves(self, live_aci: Niwaki) -> None:
        dev = (
            tenant(TENANT_DEV, description="niwaki staged-mode demo")
            .bd("sandbox").set(unicast_routing=True).bind(vrf="dev")
            .vrf("dev")
        )  # fmt: skip

        report = dev.push(live_aci, mode="staged")

        assert report.request_count == 4  # tenant, bd, rsctx, vrf
        assert report.dns[0] == f"uni/tn-{TENANT_DEV}"

    # ── 3 · Day-2 declaratively: smaller designs, same verbs ─────────────────

    def test_06_day2_patch_is_a_small_design(self, live_aci: Niwaki) -> None:
        """Only the declared field travels — the rest is untouched (D-1)."""
        patch = tenant(TENANT).bd("backend").set(description="backend tier - patched declaratively")
        patch.push(live_aci)

        mo = live_aci.tenant(TENANT).bd("backend").read()
        assert mo.description == "backend tier - patched declaratively"
        assert mo.unicast_routing is True  # untouched

    def test_07_plan_writes_only_on_drift(self, live_aci: Niwaki) -> None:
        """Write only on drift, declaratively: the plan says when to push."""
        desired = tenant(TENANT).bd("database").set(arp_flooding=True)
        assert not desired.push(live_aci, mode="plan").has_changes  # already there

        flipped = tenant(TENANT).bd("database").set(arp_flooding=False)
        plan = flipped.push(live_aci, mode="plan")
        assert plan.updates == {f"uni/tn-{TENANT}/BD-database": {"arp_flooding": (True, False)}}
        flipped.push(live_aci)
        assert not flipped.push(live_aci, mode="plan").has_changes

    def test_08_new_subtree_is_a_design_too(self, live_aci: Niwaki) -> None:
        """A BD + subnet + VRF reference — closed world, so the existing VRF
        is re-declared as an attribute-less upsert (D-1)."""
        cfg = tenant(TENANT)
        cfg.vrf("prod")  # upsert — already exists, nothing changes
        cfg.bd("typed-path", unicast_routing=True).subnet("10.10.9.1/24", scope="private").bind(
            vrf="prod"
        )
        cfg.push(live_aci)

        assert live_aci.tenant(TENANT).bd("typed-path").query("fvSubnet").count() == 1

    # ── 4 · The bridge to acts 1-2: domain attach + static VPC path ──────────

    def test_09_epgs_attach_to_the_domain(self, live_aci: Niwaki) -> None:
        """``fvRsDomAtt`` links each EPG to act-2's physical domain.

        Two equivalent spellings: declare the phys-dom in the design and
        ``bind(domain=...)`` (closed world, abstract target), or reference it
        by raw DN with ``bind_dn`` (D-3b) — no lookup, the APIC arbitrates.
        """
        cfg = tenant(TENANT)
        cfg.phys_dom(PHYS_DOM)  # cross-domain upsert of act-2's domain
        app = cfg.app("shop")
        app.epg("frontend").bind(domain=PHYS_DOM)
        app.epg("backend").bind(domain=PHYS_DOM)
        app.epg("database").bind_dn(domain=PHYS_DOM_DN)
        cfg.push(live_aci)

        assert live_aci.tenant(TENANT).query("fvRsDomAtt").count() == 3

    def test_10_static_vpc_path_binding(self, live_aci: Niwaki) -> None:
        """``static_path`` maps the frontend EPG on the act-2 VPC, tagged
        vlan-201 — the everyday "plug a workload" operation, as a literal-DN
        maker (the path DN lives outside ``uni``)."""
        cfg = tenant(TENANT)
        cfg.app("shop").epg("frontend").static_path(VPC_PATH_DN, encap="vlan-201", mode="regular")
        cfg.push(live_aci)

        paths = live_aci.tenant(TENANT).query("fvRsPathAtt").fetch()
        assert [p.encap for p in paths] == ["vlan-201"]

    # ── 5 · Escape hatch + audit ──────────────────────────────────────────────

    def test_11_mo_escape_hatch(self, live_aci: Niwaki) -> None:
        cfg = tenant(TENANT)
        cfg.mo(tagTag, key="managed-by", value="niwaki-sdk")
        cfg.push(live_aci)

        tags = live_aci.tenant(TENANT).query("tagTag").fetch()
        assert any(t.key == "managed-by" for t in tags)

    def test_12_query_inventory(self, live_aci: Niwaki) -> None:
        tn = live_aci.tenant(TENANT)
        assert tn.query(fvBD).count() == 4  # frontend, backend, database, typed-path
        assert tn.query("fvSubnet").count() == 3  # the L2 database BD has none
        providers = {rs.name for rs in tn.query("fvRsProv").fetch()}
        assert providers == {"web-api", "db"}

    # ── 99 · Cleanup of the entire walkthrough (acts 3 → 2 → 1) ──────────────

    def test_99_cleanup_everything(self, live_aci: Niwaki) -> None:
        wipe_all(live_aci)
        for name in (TENANT, TENANT_DEV):
            with pytest.raises(NotFoundError):
                live_aci.tenant(name).read()
        with pytest.raises(NotFoundError):
            live_aci.root.infra().aaep("niwaki-aaep").read()
        with pytest.raises(NotFoundError):
            live_aci.root.fabric().dns_profile("niwaki-dns").read()
