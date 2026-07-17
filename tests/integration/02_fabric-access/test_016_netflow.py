"""Fabric access — NetFlow interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_016_netflow.py -m integration -s

The NetFlow shelf: record policies across a spread of collect/match parameter
combinations; exporter policies across the version x source-IP-type cartesian
(each binding a reference VRF and EPG for reachability, DSCP swept); and monitor
policies binding an exporter and a record. The exporter reachability targets are
closed over an in-design reference tenant. Values are illustrative and cover the
SDK surface, not a real telemetry plan.

This file owns only its niwaki-it-* policies (and one reference tenant); wipe(aci)
removes them and is run by hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, infra
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

REF_TN = "niwaki-it-nf-ref"
REF_VRF = "niwaki-it-nf-vrf"
REF_BD = "niwaki-it-nf-bd"
REF_AP = "niwaki-it-nf-ap"
REF_EPG = "niwaki-it-nf-epg"

# Record collect/match combinations (AnalyticsCollectParams / AnalyticsMatchParams).
RECORDS: tuple[tuple[str, str, str], ...] = (
    ("ipv4", "count-pkts,count-bytes", "src-ipv4,dst-ipv4,proto"),
    ("ipv6", "count-pkts,count-bytes,ts-first,ts-recent", "src-ipv6,dst-ipv6"),
    ("l2", "count-pkts", "src-mac,dst-mac,ethertype,vlan"),
    ("ports", "count-pkts,tcp-flags", "src-ip,dst-ip,src-port,dst-port"),
    (
        "full",
        "count-bytes,count-pkts,sampler-id,pkt-disp,ts-first,ts-recent,tcp-flags,src-intf",
        "src-ip,dst-ip,proto,tos",
    ),
    ("min", "src-intf", "unspecified"),
)

# The 6.0(9c) simulator rejects export versions other than v9 ("Only Version 9
# supported"), so the version enum sweep collapses to v9 on this platform.
VERSIONS = ("v9",)
SOURCE_IP_TYPES = ("custom-src-ip", "inband-mgmt-ip", "oob-mgmt-ip", "ptep")
DSCP_VALUES: tuple[int | str, ...] = (0, 10, 18, 26, 34, 46, 44, "CS2")

VERSION_SLUG = {"cisco-v1": "ciscov1", "v5": "v5", "v9": "v9"}
SRCIP_SLUG = {
    "custom-src-ip": "custom",
    "inband-mgmt-ip": "inband",
    "oob-mgmt-ip": "oob",
    "ptep": "ptep",
}

# Monitor bindings: (slug, exporter version, exporter source-ip, record slug).
MONITORS: tuple[tuple[str, str, str, str], ...] = (
    ("ipv4", "v9", "oob-mgmt-ip", "ipv4"),
    ("ipv6", "v9", "inband-mgmt-ip", "ipv6"),
    ("full", "v9", "ptep", "full"),
)


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _record_name(slug: str) -> str:
    return f"niwaki-it-nf-rec-{slug}"


def _exporter_name(version: str, srcip: str) -> str:
    return f"niwaki-it-nf-exp-{VERSION_SLUG[version]}-{SRCIP_SLUG[srcip]}"


def _monitor_name(slug: str) -> str:
    return f"niwaki-it-nf-mon-{slug}"


def _dscp_exporter_name(dscp: int | str) -> str:
    return f"niwaki-it-nf-exp-dscp-{str(dscp).lower()}"


def _reference_tenant(fab: Cursor) -> None:
    """Declare the reference tenant VRF + BD + AP + EPG for exporter binds."""
    tn = fab.tenant(REF_TN, description="Reference targets for NetFlow exporters.")
    tn.vrf(REF_VRF, description="Reference VRF the collector sits behind.")
    tn.bd(REF_BD, description="Reference bridge domain.").bind(vrf=REF_VRF)
    tn.app(REF_AP, description="Reference application profile.").epg(
        REF_EPG, description="Reference EPG in front of the collector."
    ).bind(bd=REF_BD)


def test_netflow_records(live_aci: Niwaki) -> None:
    """Record policies across collect/match parameter combinations."""
    fab = infra()
    for slug, collect, match in RECORDS:
        rec = fab.netflow_record(
            _record_name(slug),
            collect_params=collect,
            match_params=match,
            description=f"NetFlow record collect/match combo - {slug}.",
        )
        _common(rec)
    fab.push(live_aci)


def test_netflow_exporters(live_aci: Niwaki) -> None:
    """Exporter policies: version x source-IP-type, each binding VRF + EPG."""
    fab = infra()
    _reference_tenant(fab)
    idx = 0
    for version in VERSIONS:
        for srcip in SOURCE_IP_TYPES:
            exp = fab.netflow_exporter(
                _exporter_name(version, srcip),
                remote_entity_ip=f"10.0.0.{10 + idx}",
                remote_entity_l4_port=2055 + idx,
                source_ip_type=srcip,
                # A custom source IP must be a subnet with a mask no longer than /20.
                source_ip_address="192.0.0.0/20" if srcip == "custom-src-ip" else None,
                exporter_netflow_version_format=version,
                qos_dscp_value=DSCP_VALUES[idx % len(DSCP_VALUES)],
                description=f"NetFlow exporter matrix - {version}, source {srcip}.",
            )
            _common(exp)
            exp.bind(vrf=REF_VRF)
            exp.bind(epg=REF_EPG)
            idx += 1
    fab.push(live_aci)


def test_netflow_exporter_dscp(live_aci: Niwaki) -> None:
    """One exporter per DSCP value — the DSCP field factored across instances.

    Export version is pinned to v9 (the platform limit), so the DSCP enum is swept
    on separate exporters rather than cartesian'd with version.
    """
    fab = infra()
    _reference_tenant(fab)
    for idx, dscp in enumerate(DSCP_VALUES):
        exp = fab.netflow_exporter(
            _dscp_exporter_name(dscp),
            remote_entity_ip=f"10.0.1.{10 + idx}",
            remote_entity_l4_port=6343,
            source_ip_type="oob-mgmt-ip",
            exporter_netflow_version_format="v9",
            qos_dscp_value=dscp,
            description=f"NetFlow exporter DSCP sweep - {dscp}.",
        )
        _common(exp)
        exp.bind(vrf=REF_VRF)
        exp.bind(epg=REF_EPG)
    fab.push(live_aci)


def test_netflow_monitors(live_aci: Niwaki) -> None:
    """Monitor policies binding an exporter and a record (closed in-design)."""
    fab = infra()
    _reference_tenant(fab)

    for slug, collect, match in RECORDS:
        rec = fab.netflow_record(
            _record_name(slug),
            collect_params=collect,
            match_params=match,
            description=f"NetFlow record collect/match combo - {slug}.",
        )
        _common(rec)

    exporters_needed = {(v, s) for _, v, s, _ in MONITORS}
    for version, srcip in exporters_needed:
        exp = fab.netflow_exporter(
            _exporter_name(version, srcip),
            remote_entity_ip="10.0.0.9",
            remote_entity_l4_port=2055,
            source_ip_type=srcip,
            exporter_netflow_version_format=version,
            description=f"NetFlow exporter matrix - {version}, source {srcip}.",
        )
        _common(exp)
        exp.bind(vrf=REF_VRF)
        exp.bind(epg=REF_EPG)

    for slug, version, srcip, rec_slug in MONITORS:
        mon = fab.netflow_monitor(
            _monitor_name(slug),
            description=f"NetFlow monitor - binds exporter and record, {slug}.",
        )
        _common(mon)
        mon.bind(netflow_exporter=_exporter_name(version, srcip))
        mon.bind(netflow_record=_record_name(rec_slug))

    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    record_dns = [f"uni/infra/recordpol-{_record_name(s)}" for s, _, _ in RECORDS]
    exporter_dns = [
        f"uni/infra/exporterpol-{_exporter_name(v, s)}" for v in VERSIONS for s in SOURCE_IP_TYPES
    ]
    dscp_exporter_dns = [f"uni/infra/exporterpol-{_dscp_exporter_name(d)}" for d in DSCP_VALUES]
    monitor_dns = [f"uni/infra/monitorpol-{_monitor_name(s)}" for s, _, _, _ in MONITORS]
    for dn in (*record_dns, *exporter_dns, *dscp_exporter_dns, *monitor_dns, f"uni/tn-{REF_TN}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
