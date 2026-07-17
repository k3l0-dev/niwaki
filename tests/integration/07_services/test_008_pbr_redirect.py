"""L4-L7 services — policy-based redirect, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_008_pbr_redirect.py -m integration -s

The operator builds the PBR service container (``vnsSvcCont``) and sweeps the redirect
policies (``vnsSvcRedirectPol``) across the cartesian of hashing algorithm x
threshold-down action for L3 destinations, rotating the anycast / resilient-hashing /
source-MAC-rewrite / threshold-enable / local-pod booleans, plus one L1 and one L2
redirect policy to cover the destination-type enum. Each L3 policy carries redirect
destinations bound to a health group and an IP-SLA / backup policy. Backup policies,
health groups and both service-EPG preferred-group states are also declared.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) drops the
dedicated tenant.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:vnsInstPol@vnsSvcCont (L4-L7 device policy)
#   - maker:vnsL1L2RedirectDest@vnsSvcRedirectPol / vnsL1L2RedirectDest@vnsBackupPol
#     (L1/L2 destinations have no maker — the L1/L2 policies below carry no destination)
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-pbr"

HASHING = ("dip", "sip", "sip-dip-prototype")
THRESHOLD_ACTIONS = ("bypass", "deny", "permit")
HEALTH_GROUPS = ("niwaki-it-hg0", "niwaki-it-hg1", "niwaki-it-hg2")
BACKUP = "niwaki-it-bkp"
SLA = "niwaki-it-sla"


def test_pbr_redirect(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="PBR redirect: hashing x threshold x destination matrix.")

    tn.ip_sla_monitoring_policy(
        SLA, description="ICMP SLA the redirect policies track.", sla_type="icmp", frequency=5
    )

    svc = tn.service_container()
    for group in HEALTH_GROUPS:
        svc.l4_l7_redirect_health_group(group, description=f"Redirect health group {group}.")

    # Backup policy with several destinations, spread across the health groups.
    backup = svc.pbr_backup_policy(BACKUP, description="Backup redirect policy.")
    for bdx in range(3):
        backup_dest = backup.destination_of_redirected_traffic(
            f"10.40.0.{bdx + 9}",
            dest_name=f"backup-node-{bdx}",
            second_ip_address=f"10.40.1.{bdx + 9}",
            mac_address=f"00:11:22:33:44:{bdx:02d}",
            pod_id=1,
            weight=bdx + 1,
        )
        backup_dest.bind(l4_l7_redirect_health_group=HEALTH_GROUPS[bdx % len(HEALTH_GROUPS)])

    # Both service-EPG preferred-group states.
    for member in ("exclude", "include"):
        svc.service_epg_policy(
            f"niwaki-it-sep-{member}",
            description=f"Service-EPG policy ({member}).",
            preferred_group_member=member,
        )

    # L3 redirect policies: hashing x threshold-down action, booleans rotated.
    macrewrite_cycle = itertools.cycle((True, False))
    localpod_cycle = itertools.cycle((True, False))
    backup_bound = False
    for index, (hashing, threshold_action) in enumerate(
        itertools.product(HASHING, THRESHOLD_ACTIONS)
    ):
        # Drive anycast and resilient hashing independently so all four combinations
        # occur — crucially a non-anycast policy with resilient hashing on, the only
        # shape that attaches the backup-policy relation (combination rule #11).
        anycast = bool(index % 2)
        resilient = bool((index // 2) % 2)
        redirect = svc.service_redirect_policy(
            f"niwaki-it-rp{index:02d}",
            description=f"L3 redirect {hashing}/{threshold_action}.",
            anycast_enabled_or_not=anycast,
            dest_type="L3",
            hashing_algorithm=hashing,
            maximum_threshold_percentage=90,
            minimum_threshold_percentage=10,
            # location-aware PBR (local-pod-only) and anycast are mutually exclusive.
            program_local_pod_only=False if anycast else next(localpod_cycle),
            resilient_hashing_enabled_or_not=resilient,
            src_mac_rewrite_enabled=next(macrewrite_cycle),
            threshold_down_action=threshold_action,
            threshold_enable=True,
        )
        # IP-SLA tracking and health groups are unsupported with anycast; the backup
        # policy relation requires resilient hashing to be enabled.
        if not anycast:
            redirect.bind(ip_sla_monitoring_policy=SLA)
            if resilient:
                # A backup policy can be referenced by only one redirect policy: the
                # first resilient policy uses the shared BACKUP, the rest get their own.
                if not backup_bound:
                    redirect.bind(pbr_backup_policy=BACKUP)
                    backup_bound = True
                else:
                    extra_backup = svc.pbr_backup_policy(
                        f"niwaki-it-bkp{index}",
                        description="Per-policy backup redirect policy.",
                    )
                    extra_dest = extra_backup.destination_of_redirected_traffic(
                        f"10.43.{index}.9",
                        dest_name=f"backup-node-{index}",
                        mac_address=f"00:11:22:33:77:{index:02d}",
                        pod_id=1,
                        weight=1,
                    )
                    extra_dest.bind(
                        l4_l7_redirect_health_group=HEALTH_GROUPS[index % len(HEALTH_GROUPS)]
                    )
                    redirect.bind(pbr_backup_policy=f"niwaki-it-bkp{index}")
        dest = redirect.destination_of_redirected_traffic(
            f"10.41.{index}.1",
            dest_name=f"fw-node-{index}",
            second_ip_address=f"10.41.{index}.2",
            mac_address=f"00:11:22:33:55:{index:02d}",
            pod_id=1,
            weight=(index % 4) + 1,
        )
        if not anycast:
            dest.bind(l4_l7_redirect_health_group=HEALTH_GROUPS[index % len(HEALTH_GROUPS)])
            # A second destination on the same policy, bound to a different group.
            dest2 = redirect.destination_of_redirected_traffic(
                f"10.42.{index}.1",
                dest_name=f"fw-node-b-{index}",
                second_ip_address=f"10.42.{index}.2",
                mac_address=f"00:11:22:33:66:{index:02d}",
                pod_id=1,
                weight=(index % 4) + 2,
            )
            dest2.bind(l4_l7_redirect_health_group=HEALTH_GROUPS[(index + 1) % len(HEALTH_GROUPS)])

    # L1 and L2 redirect policies cover the remaining dest_type values (L1/L2
    # destinations have no maker, so these carry none — see COVERAGE GAPS).
    svc.service_redirect_policy(
        "niwaki-it-rp-l1",
        description="L1 redirect policy.",
        dest_type="L1",
        hashing_algorithm="sip-dip-prototype",
        threshold_enable=False,
    )
    svc.service_redirect_policy(
        "niwaki-it-rp-l2",
        description="L2 redirect policy.",
        dest_type="L2",
        hashing_algorithm="sip-dip-prototype",
        threshold_enable=False,
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
