"""Observability — tenant NetFlow, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_008_netflow_tenant.py -m integration -s

The operator builds tenant NetFlow: records over several collect/match key sets,
exporters bound to the VRF and EPG behind which the collector resides (over a
spread of source-IP types and DSCP marks), and monitors tying each record to an
exporter. Only NetFlow version 9 is accepted by the APIC, so every exporter pins it.

Exhaustive, non-prod. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS: universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no
#   maker. NetFlow version variety is impossible — the APIC accepts version 9 only.

# The same NetFlow surface is factored across several tenants (namespace independence).
TENANTS = ("niwaki-it-obs-nf", "niwaki-it-obs-nf2", "niwaki-it-obs-nf3")
EPGS = ("web", "app", "db")
# (name, collect_params, match_params).
RECORDS = (
    ("rec-v4", "count-bytes,count-pkts,tcp-flags", "src-ipv4,dst-ipv4,src-port,dst-port,proto"),
    ("rec-v6", "count-bytes,count-pkts,pkt-disp", "src-ipv6,dst-ipv6,src-port,dst-port"),
    ("rec-l2", "count-bytes,ts-first,ts-recent", "ethertype,src-mac,dst-mac,vlan"),
    ("rec-ip", "count-pkts,sampler-id", "src-ip,dst-ip,tos"),
)
# (source_ip_type, dscp).
EXPORTERS = (
    ("custom-src-ip", 0),
    ("oob-mgmt-ip", 10),
    ("inband-mgmt-ip", 46),
)


def _build_tenant_netflow(cfg: Cursor, tn: str) -> None:
    """Build the full tenant NetFlow surface (records, exporters, monitors) under *tn*."""
    tenant = cfg.tenant(tn, description="Observability: tenant NetFlow records/exporters/monitors.")
    tenant.vrf("vrf")
    tenant.bd("bd").bind(vrf="vrf")
    app = tenant.app("ap")
    for epg in EPGS:
        app.epg(epg).bind(bd="bd")

    for name, collect, match in RECORDS:
        tenant.netflow_record(
            name,
            description=f"Tenant NetFlow record {name}.",
            collect_params=collect,
            match_params=match,
        )

    for i, (src_type, dscp) in enumerate(EXPORTERS):
        kwargs = {}
        if src_type == "custom-src-ip":
            kwargs["source_ip_address"] = "10.31.0.1/20"  # unicast host, mask <= /20
        tenant.netflow_exporter(
            f"exp-{i}",
            description=f"Tenant NetFlow exporter, {src_type}, dscp {dscp}.",
            remote_entity_ip=f"10.31.{i}.9",
            remote_entity_l4_port=2055 + i,
            source_ip_type=src_type,
            qos_dscp_value=dscp,
            exporter_netflow_version_format="v9",  # the APIC accepts version 9 only
            **kwargs,
        ).bind(vrf="vrf", epg=EPGS[i % len(EPGS)])

    for i, (name, _collect, _match) in enumerate(RECORDS):
        tenant.netflow_monitor(f"mon-{name}", description=f"Monitor for record {name}.").bind(
            netflow_record=name, netflow_exporter=f"exp-{i % len(EXPORTERS)}"
        )


def test_tenant_netflow(live_aci: Niwaki) -> None:
    """The same NetFlow surface, factored across several tenants."""
    cfg = design()
    for tn in TENANTS:
        _build_tenant_netflow(cfg, tn)
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for tn in TENANTS:  # each tenant delete cascades its records/exporters/monitors
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/tn-{tn}").delete()
