"""Observability — access NetFlow, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_007_netflow_access.py -m integration -s

The operator builds access (infra) NetFlow: a record per collect-parameter flag and
a record per match-parameter flag (plus multi-flag combinations), an exporter per
source-IP type (the custom type sourced from a unicast subnet, the management types
reachable over in-band/out-of-band), monitors tying records to exporters, node
policies over the MTU space, and a VMM exporter. Only NetFlow version 9 is accepted
by the APIC, so every exporter pins it.

Exhaustive, non-prod. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS: universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no
#   maker. NetFlow version variety is impossible — the APIC accepts version 9 only.

# Each collect/match parameter flag, exercised on its own record.
COLLECT_FLAGS = (
    "count-bytes",
    "count-pkts",
    "sampler-id",
    "pkt-disp",
    "ts-first",
    "ts-recent",
    "tcp-flags",
    "src-intf",
)
MATCH_FLAGS = (
    "ethertype",
    "dst-mac",
    "src-mac",
    "vlan",
    "proto",
    "tos",
    "src-ipv4",
    "dst-ipv4",
    "src-ipv6",
    "dst-ipv6",
    "src-port",
    "dst-port",
    "src-ip",
    "dst-ip",
)
# One exporter per source-IP type (custom needs a unicast address with mask <= /20).
SOURCE_IP_TYPES = ("custom-src-ip", "inband-mgmt-ip", "oob-mgmt-ip", "ptep")
DSCP_VALUES = (0, 10, 46)
NODE_MTUS = (1500, 9000)


def _slug(flag: str) -> str:
    """A DN-safe fragment for a flag value."""
    return flag.replace("-", "")


def test_access_netflow_records(live_aci: Niwaki) -> None:
    """One record per collect flag and per match flag, plus combinations."""
    cfg = design()
    infra = cfg.infra()
    for flag in COLLECT_FLAGS:
        infra.netflow_record(
            f"rec-c-{_slug(flag)}",
            description=f"Record collecting {flag}.",
            collect_params=flag,
            match_params="src-ip,dst-ip",
        )
    for flag in MATCH_FLAGS:
        infra.netflow_record(
            f"rec-m-{_slug(flag)}",
            description=f"Record matching {flag}.",
            collect_params="count-bytes,count-pkts",
            match_params=flag,
        )
    infra.netflow_record(
        "rec-combo-v4",
        description="Record: byte/packet/flag collection over the IPv4 5-tuple.",
        collect_params="count-bytes,count-pkts,tcp-flags,pkt-disp",
        match_params="src-ipv4,dst-ipv4,src-port,dst-port,proto",
    )
    infra.netflow_record(
        "rec-combo-l2",
        description="Record: byte collection over the L2 key.",
        collect_params="count-bytes,ts-first,ts-recent",
        match_params="ethertype,src-mac,dst-mac,vlan",
    )
    cfg.push(live_aci)


def test_access_netflow_exporters(live_aci: Niwaki) -> None:
    """One exporter per source-IP type, over a spread of DSCP marks."""
    cfg = design()
    infra = cfg.infra()
    for i, src_type in enumerate(SOURCE_IP_TYPES):
        kwargs = {}
        if src_type == "custom-src-ip":
            kwargs["source_ip_address"] = "10.30.0.1/20"  # unicast host, mask <= /20
        infra.netflow_exporter(
            f"exp-{_slug(src_type)}",
            description=f"Exporter sourced from {src_type}.",
            remote_entity_ip=f"10.30.{i}.9",
            remote_entity_l4_port=2055 + i,
            source_ip_type=src_type,
            qos_dscp_value=DSCP_VALUES[i % len(DSCP_VALUES)],
            exporter_netflow_version_format="v9",  # the APIC accepts version 9 only
            **kwargs,
        )
    cfg.push(live_aci)


def test_access_netflow_monitors(live_aci: Niwaki) -> None:
    """Monitors tying records to exporters, node policies and a VMM exporter."""
    cfg = design()
    infra = cfg.infra()

    # Self-contained record + exporters the monitors reference.
    infra.netflow_record(
        "mon-rec",
        description="Monitor record.",
        collect_params="count-bytes,count-pkts",
        match_params="src-ip,dst-ip",
    )
    infra.netflow_exporter(
        "mon-exp-a",
        description="Monitor exporter A, out-of-band sourced.",
        remote_entity_ip="10.32.0.9",
        remote_entity_l4_port=2055,
        source_ip_type="oob-mgmt-ip",
        exporter_netflow_version_format="v9",
    )
    infra.netflow_exporter(
        "mon-exp-b",
        description="Monitor exporter B, in-band sourced.",
        remote_entity_ip="10.32.0.10",
        remote_entity_l4_port=4739,
        source_ip_type="inband-mgmt-ip",
        exporter_netflow_version_format="v9",
    )
    infra.netflow_monitor("mon-single", description="Monitor with one exporter.").bind(
        netflow_record="mon-rec", netflow_exporter="mon-exp-a"
    )
    infra.netflow_monitor("mon-dual", description="Monitor with two exporters.").bind(
        netflow_record="mon-rec"
    ).bind(netflow_exporter="mon-exp-a").bind(netflow_exporter="mon-exp-b")

    for mtu in NODE_MTUS:
        infra.netflow_node_policy(
            f"node-mtu-{mtu}",
            description=f"NetFlow node policy, MTU {mtu}.",
            collection_interval_in_seconds=300,
            template_interval_in_seconds=600,
            mtu=mtu,
        )
    infra.netflow_vmm_exporter(
        "vmmexp",
        description="NetFlow VMM exporter for virtual-switch flow export.",
        remote_entity_ip="10.33.0.9",
        remote_entity_l4_port=4739,
        source_ip_address="10.33.0.1",  # the VMM exporter wants a bare host address
        vmm_exporter_netflow_version_format="v9",
    )

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns: list[str] = []
    for flag in COLLECT_FLAGS:
        dns.append(f"uni/infra/recordpol-rec-c-{_slug(flag)}")
    for flag in MATCH_FLAGS:
        dns.append(f"uni/infra/recordpol-rec-m-{_slug(flag)}")
    dns += [
        "uni/infra/recordpol-rec-combo-v4",
        "uni/infra/recordpol-rec-combo-l2",
        "uni/infra/recordpol-mon-rec",
    ]
    for src_type in SOURCE_IP_TYPES:
        dns.append(f"uni/infra/exporterpol-exp-{_slug(src_type)}")
    dns += [
        "uni/infra/exporterpol-mon-exp-a",
        "uni/infra/exporterpol-mon-exp-b",
        "uni/infra/monitorpol-mon-single",
        "uni/infra/monitorpol-mon-dual",
        "uni/infra/vmmexporterpol-vmmexp",
    ]
    for mtu in NODE_MTUS:
        dns.append(f"uni/infra/nodepol-node-mtu-{mtu}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
