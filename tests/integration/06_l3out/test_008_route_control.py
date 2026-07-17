"""External connectivity — route-control match and set clauses, exhaustive (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_008_route_control.py -m integration -s

Route-maps are pure tenant policy, so this file cartesians them widely. It builds
match rules with every match term (prefix lists with length windows, community
terms and their factors over both community scopes, community and AS-path regular
expressions over their type enums) and action rules that sweep every ``set``
clause across its enums (AS-path prepend criteria + ASN entries, community
add/set criteria, dampening, next-hop, policy tag, preference, metric, metric
type, redistribute-multipath, route tag, weight). It then assembles reusable
route-maps (``rtctrlProfile`` combinable/global) with ordered permit/deny
contexts referencing those rules.

No physical resources needed. Values are illustrative. ``wipe(aci)`` is
operator-only.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.design._cursor import Cursor
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-l3out"


def _match_rules(t: Cursor) -> list[str]:
    """Build a spread of match rules; return their names."""
    names: list[str] = []
    # A rule with every match term.
    mr = t.match_rule("niwaki-it-match-all", description="Prefix/community/AS-path match rule.")
    mr.match_prefix("10.0.0.0/8", aggregated_route=False, description="Specific prefix.")
    mr.match_prefix(
        "192.168.0.0/16",
        aggregated_route=True,
        start_of_prefix_length=17,
        end_of_prefix_length=24,
        description="RFC1918 /17..24 window.",
    )
    for scope in ("transitive", "non-transitive"):
        mr.match_community(f"comm-{scope}").factor(
            f"regular:as2-nn2:65000:{100 if scope == 'transitive' else 200}",
            scope=scope,
            description=f"Community factor scope {scope}.",
        )
    mr.match_as_path("aspath", regular_expression="^65001_", description="AS-path regex.")
    names.append("niwaki-it-match-all")

    # Community-regex terms live in their own match rule: the APIC forbids regex and
    # non-regex community terms in the same subject profile for a community type.
    regex = t.match_rule("niwaki-it-match-regex", description="Community-regex match rule.")
    for ct in ("regular", "extended"):
        regex.match_community_regex(
            ct, regular_expression="_65000_", description=f"Community regex {ct}."
        )
    names.append("niwaki-it-match-regex")

    # Prefix-only rules over several length windows.
    for i, (net, start, end) in enumerate(
        [("172.16.0.0/12", 13, 24), ("100.64.0.0/10", 11, 32), ("2001:db8::/32", 33, 64)]
    ):
        name = f"niwaki-it-match-pfx-{i}"
        r = t.match_rule(name, description=f"Prefix window {net}.")
        r.match_prefix(
            net,
            aggregated_route=True,
            start_of_prefix_length=start,
            end_of_prefix_length=end,
            description=f"Window {start}..{end}.",
        )
        names.append(name)
    return names


def _action_rules(t: Cursor) -> list[str]:
    """Build action rules sweeping every set clause across its enums; return names."""
    names: list[str] = []
    aspath_crit = ["prepend", "prepend-last-as"]
    comm_crit = ["append", "none", "replace"]
    metric_type = ["ospf-type1", "ospf-type2"]

    for i in range(len(comm_crit)):
        name = f"niwaki-it-action-{i}"
        ar = t.action_rule_profile(name, description=f"Action rule variant {i}.")
        # last_as_number must be 0 for the plain prepend criterion, non-zero only
        # for prepend-last-as.
        crit = aspath_crit[i % len(aspath_crit)]
        ar.set_as_path(
            crit,
            last_as_number=(0 if crit == "prepend" else 2),
            description=f"AS-path {crit}.",
        ).asn(1, as_number=65001 + i, description="Prepended ASN.")
        # add-community only supports the "append" criterion.
        ar.add_community("no-advertise", set_criteria="append", description="Add community.")
        # set-community with criterion "none" clears communities, so it carries no
        # community value; the other criteria set an explicit community.
        if comm_crit[i] == "none":
            ar.set_community(set_criteria="none", description="Clear communities.")
        else:
            ar.set_community(
                community="no-export",
                set_criteria=comm_crit[i],
                description=f"Set community, {comm_crit[i]}.",
            )
        ar.set_metric_type(
            metric_type=metric_type[i % len(metric_type)], description="Metric type."
        )
        ar.set_dampening(
            half_life=15,
            reuse_limit=750,
            suppress_limit=2000,
            max_suppress_time=60,
            description="Dampening.",
        )
        ar.set_next_hop(addr=f"10.255.255.{i + 1}", description="Next hop.")
        ar.set_policy_tag(description="Policy tag.")
        ar.set_preference(local_pref=100 + i * 10, description="Local preference.")
        ar.set_metric(metric=100 + i, description="MED.")
        ar.set_route_tag(route_tag=1000 + i, description="Route tag.")
        ar.set_weight(weight=200 + i, description="Weight.")
        names.append(name)

    # Next-hop-propagation clauses form their own rule: set_next_hop_unchanged is
    # mutually exclusive with set_route_tag / an explicit next hop, and
    # set_redistribute_multipath requires next-hop propagation to be present.
    nhu = t.action_rule_profile(
        "niwaki-it-action-nhu", description="Action rule: next-hop propagation."
    )
    nhu.set_next_hop_unchanged(description="Next hop unchanged.")
    nhu.set_redistribute_multipath(description="Redistribute multipath.")
    names.append("niwaki-it-action-nhu")
    return names


def test_match_and_action_rules(live_aci: Niwaki) -> None:
    """The reusable match and action rules (every term / set clause across its enums)."""
    t = tenant(TN)
    _match_rules(t)
    _action_rules(t)
    t.push(live_aci)


def test_route_maps(live_aci: Niwaki) -> None:
    """Reusable route-maps (both types) with ordered permit/deny contexts + scopes."""
    t = tenant(TN)
    matches = _match_rules(t)
    actions = _action_rules(t)

    order = 1
    for kind in ("combinable", "global"):
        rmap = t.route_control_profile(
            f"niwaki-it-rmap-{kind}",
            type=kind,
            auto_continue=(kind == "combinable"),
            description=f"{kind} route-map.",
        )
        for idx, verb in enumerate(("permit", "deny")):
            ctx = rmap.route_control_context(
                f"ctx-{idx}",
                action=verb,
                local_order=order,
                description=f"Context {idx} ({verb}).",
            )
            ctx.bind(match_rule=matches[idx])
            ctx.route_context_scope().bind(action_rule_profile=actions[idx])
            order = order + 1 if order < 9 else 1

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's route-control objects."""
    dns = [f"uni/tn-{TN}/prof-niwaki-it-rmap-combinable", f"uni/tn-{TN}/prof-niwaki-it-rmap-global"]
    dns += [f"uni/tn-{TN}/subj-niwaki-it-match-all", f"uni/tn-{TN}/subj-niwaki-it-match-regex"]
    dns += [f"uni/tn-{TN}/subj-niwaki-it-match-pfx-{i}" for i in range(3)]
    dns += [f"uni/tn-{TN}/attr-niwaki-it-action-{i}" for i in range(3)]
    dns += [f"uni/tn-{TN}/attr-niwaki-it-action-nhu"]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
