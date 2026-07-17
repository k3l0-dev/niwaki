"""Coverage audit — curated parents whose children stay unreachable from the DSL.

The design vocabulary curates a subset of the 2 222 generated ACI classes.  A
*gap* is a child of an **already-curated** parent that a user still cannot
express through the DSL:

- a **maker gap** — the child is creatable (``_is_creatable`` is true) and
  carries configurable fields, yet no maker reaches it;
- a **bind gap** — the child is a relation (``fooRsBar``) with no bind alias,
  verb, or reference resolving to it.

Detection is **mechanical and complete**: :func:`scan_gaps` walks every curated
parent in the vocabulary and reports every child that is neither covered nor
pure metadata.  It subtracts the four ways the DSL reaches a child — makers,
bind aliases (through ``REFERENCE_MAP`` / ``TARGET_SUBCLASSES``) and verbs — so a
relation reachable by ``.provide()`` is *not* a gap.

Whether a detected gap **should** be curated is a judgement call, recorded in
:data:`SCOPE_RULES`.  Each rule carries a reason and a bucket:

- ``out`` — will never be curated (an *action* rather than desired state; the
  surface of a different controller; or empirically rejected by the target
  APIC);
- ``deferred`` — real config, but only meaningful against a backend we cannot
  exercise on the simulator (VMM/vCenter, Intersight, an on-switch app);
- everything unmatched defaults to ``in`` — the in-scope backlog.

The default is **in**, on purpose: a newly generated class that becomes a gap is
*visible* until a human writes down why it is out.  That is the safe direction —
exclusions are enumerated and reasoned, never inferred by a name pattern alone.

Run ``python -m niwaki._codegen.coverage_audit`` for the grouped report, or
``--json`` for the machine snapshot the drift guard compares against.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from niwaki.design._cursor import _load_class, _tables
from niwaki.domain import _child_map as cm

#: Children that are pure metadata — never a real curation gap.
_METADATA_CHILDREN = frozenset(
    {"tagAnnotation", "aaaRbacAnnotation", "tagTag", "tagInst", "tagExtMngdInst"}
)

#: Model fields that are ambient (not user-authored config) — ignored when
#: deciding whether a creatable child is worth a maker.
_AMBIENT_FIELDS = frozenset(
    {"children", "annotation", "userdom", "display_name", "owner_key", "owner_tag"}
)

Bucket = Literal["out", "deferred", "in"]


@dataclass(frozen=True, slots=True)
class Gap:
    """A single curated-parent → uncurated-child edge.

    :param parent: the curated parent ACI class (a vocabulary position).
    :param child: the child ACI class the DSL cannot reach.
    :param kind: ``"maker"`` for a creatable child, ``"bind"`` for a relation.
    """

    parent: str
    child: str
    kind: Literal["maker", "bind"]

    @property
    def key(self) -> str:
        """A stable, sortable identity: ``"maker:fabricRsLeCardP@fabricLeNodePGrp"``."""
        return f"{self.kind}:{self.child}@{self.parent}"


@dataclass(frozen=True, slots=True)
class ScopeRule:
    """A curated exclusion: why a family or class is out of, or deferred from, scope.

    Exactly one of :attr:`pkg`, :attr:`pattern`, :attr:`cls` selects the target.

    :param bucket: ``"out"`` (never curated) or ``"deferred"`` (backend-gated).
    :param reason: the human justification, shown in the report.
    :param pkg: match every class of a ``classPkg`` (the lowercase name prefix).
    :param pattern: match class names against this anchored regex.
    :param cls: match one exact class name.
    """

    bucket: Bucket
    reason: str
    pkg: str | None = None
    pattern: str | None = None
    cls: str | None = None

    def matches(self, child: str) -> bool:
        """Return whether *child* is selected by this rule."""
        if self.cls is not None:
            return child == self.cls
        if self.pkg is not None:
            return _pkg(child) == self.pkg
        if self.pattern is not None:
            return re.match(self.pattern, child) is not None
        return False


# ── The curated scope judgement ──────────────────────────────────────────────
#
# Ordered: the first matching rule wins, so a specific ``in`` carve-out can
# precede a broad ``out`` family (used for eqptdiagp below).  Anything unmatched
# is in-scope backlog.

SCOPE_RULES: tuple[ScopeRule, ...] = (
    # ── OUT: actions, not desired state ─────────────────────────────────────
    # A design is idempotent — re-applying it must be a no-op.  These objects
    # *do* something when created (count, trace, dump, run a test), so they are
    # imperative triggers, not fabric state.  (Atomic counters and traceroute
    # were already excluded on this exact principle.)
    ScopeRule("out", "action — atomic counters, not desired state", pkg="dbgac"),
    ScopeRule("out", "action — traceroute, not desired state", pkg="traceroutep"),
    ScopeRule("out", "action — on-demand techsupport export", pkg="dbgexp"),
    ScopeRule("out", "action — troubleshooting session/report", pkg="troubleshoot"),
    ScopeRule("out", "action — on-demand debug mode", cls="dbgOngoingAcMode"),
    ScopeRule("out", "action — config dump trigger", cls="configDumpP"),
    ScopeRule("out", "action — stats export/report trigger", cls="statsExportP"),
    ScopeRule("out", "action — stats report trigger", cls="statsReportable"),
    ScopeRule("out", "action — config reconcile trigger", cls="recoveryReconcileConfigP"),
    ScopeRule("out", "action — on-device local maint install", cls="maintLocalInstall"),
    ScopeRule("out", "action — on-demand maint policy", cls="maintMaintPOnD"),
    ScopeRule("out", "action — on-demand diagnostic test set", pattern=r"eqptdiagp.*Od"),
    ScopeRule("out", "action — port-to-node conversion trigger", cls="fabricPortConvertNode"),
    # ── OUT: surface of a different controller ──────────────────────────────
    # Present in the on-prem schema but authored by another product; a physical
    # APIC never instantiates them, or the orchestrator owns them and niwaki
    # writing them would fight it.
    ScopeRule("out", "Cloud Network Controller (cAPIC) — inert on a physical APIC", pkg="cloud"),
    ScopeRule("out", "orchestrator (NDO) — authored by the orchestrator", pkg="orchs"),
    ScopeRule("out", "Multi-Site (MSC/NDO) — multi-domain peering, out of scope", pkg="mdp"),
    ScopeRule("out", "NDO — orchestrator info stamped by Nexus Dashboard", cls="fvOrchsInfo"),
    ScopeRule("out", "multi-site (NDO) — per-site association", cls="fvSiteAssociated"),
    ScopeRule("out", "Cloud Network Controller — tenant cloud account", cls="fvRsCloudAccount"),
    ScopeRule("out", "SD-WAN — rejected by APIC 6.0 live", cls="fvRsCtxToSDWanVpn"),
    ScopeRule("out", "SD-WAN — subject SLA policy, out of scope", cls="vzRsSdwanPol"),
    ScopeRule(
        "out", "Multi-Site (NDO) — L3Out to multi-domain provider", cls="l3extRsOutToMdpProvP"
    ),
    ScopeRule("out", "multi-site (NDO) — intersite loopback", cls="l3extIntersiteLoopBackIfP"),
    ScopeRule(
        "out", "multi-site — intersite anycast-multicast setup", cls="fabricAnycastMulticastSetupP"
    ),
    ScopeRule("out", "internal MO — CloudSec, system/multi-site managed", cls="cloudsecIfPol"),
    # ── OUT: empirically rejected by the target APIC ────────────────────────
    ScopeRule("out", "rejected by APIC 6.0 live (telemetry server groups)", pkg="telemetry"),
    # ── DEFERRED: real config, backend we cannot exercise on the sim ─────────
    ScopeRule("deferred", "VMM — needs a live vCenter/SCVMM to verify", pkg="vmm"),
    ScopeRule("deferred", "Intersight — needs an Intersight account", pkg="intersight"),
    ScopeRule("deferred", "on-switch third-party app — needs the hosted app", pkg="thirdpartyapp"),
    ScopeRule(
        "deferred",
        "service-graph LB/NAT normalized model — redundant path, deferred",
        pattern=r"vns(LB|NAT|AddrInst)",
    ),
)


def _pkg(cls: str) -> str:
    """Return the ``classPkg`` of an ACI class name (its lowercase prefix)."""
    m = re.match(r"^([a-z0-9]+)", cls)
    return m.group(1) if m else cls


def _is_relation(cls: str) -> bool:
    """Return whether *cls* is a relation (``fooRsBar``) rather than a concrete MO."""
    return bool(re.match(r"^[a-z0-9]+Rs[A-Z]", cls))


def _covered_children(parent: str) -> set[str]:
    """Every child class the DSL already reaches from *parent*.

    Unions makers, bind targets (resolved through ``REFERENCE_MAP`` and its
    abstract-to-concrete expansion) and verb relations.
    """
    tables = _tables()
    covered: set[str] = set(tables.makers.get(parent, {}).values())

    for _alias, target in tables.binds.get(parent, {}).items():
        entry = cm.REFERENCE_MAP.get(parent, {}).get(target)
        if entry:
            covered.add(entry[0])
        for concrete in cm.TARGET_SUBCLASSES.get(target, ()):
            sub = cm.REFERENCE_MAP.get(parent, {}).get(concrete)
            if sub:
                covered.add(sub[0])

    for _verb, spec in tables.verbs.get(parent, {}).items():
        covered.add(spec["rs"])

    return covered


def _is_curatable(cls_name: str) -> bool:
    """Return whether an ACI class has a generated model at all.

    A handful of schema classes are contained by a curated parent yet are never
    emitted as models (deprecated or non-concrete, e.g. ``infraRsQosDppIfPol``).
    They appear in ``CHILD_MAP`` but a maker or bind to them could not be built,
    so they are not real gaps.
    """
    try:
        _load_class(cls_name)
    except Exception:
        return False
    return True


def _has_config_fields(cls_name: str) -> bool:
    """Return whether a creatable class carries user-authored (non-ambient) fields."""
    try:
        model = _load_class(cls_name)
    except Exception:
        return False
    if not getattr(model, "_is_creatable", False):
        return False
    naming: frozenset[str] = getattr(model, "_naming_props", frozenset())
    return any(f not in _AMBIENT_FIELDS and f not in naming for f in model.model_fields)


def scan_gaps() -> list[Gap]:
    """Return every curated-parent → uncurated-child gap, sorted by :attr:`Gap.key`.

    Walks each curated parent, subtracts the children the DSL already reaches
    (makers, binds, verbs), drops metadata children, and classifies the rest:

    - a relation (``fooRsBar``) with no bind/verb → a ``"bind"`` gap;
    - a creatable child with configurable fields and no maker → a ``"maker"`` gap.

    The result is deterministic and independent of scope: use
    :func:`classify` / :func:`in_scope_gaps` to filter by judgement.

    :returns: all gaps, sorted, ready to snapshot or group into a report.
    """
    tables = _tables()
    gaps: list[Gap] = []
    for parent in tables.makers:
        covered = _covered_children(parent)
        for _label, child in cm.CHILD_MAP.get(parent, {}).items():
            if child in _METADATA_CHILDREN or child in covered:
                continue
            if _is_relation(child):
                if _is_curatable(child):
                    gaps.append(Gap(parent, child, "bind"))
            elif _has_config_fields(child):
                gaps.append(Gap(parent, child, "maker"))
    return sorted(gaps, key=lambda g: g.key)


def classify(child: str) -> tuple[Bucket, str]:
    """Return the ``(bucket, reason)`` for a gap's child class.

    The first matching :data:`SCOPE_RULES` entry wins; unmatched classes default
    to ``("in", "in-scope backlog")``.

    :param child: the child ACI class name.
    :returns: ``("out" | "deferred" | "in", reason)``.
    """
    for rule in SCOPE_RULES:
        if rule.matches(child):
            return rule.bucket, rule.reason
    return "in", "in-scope backlog"


def in_scope_gaps(gaps: list[Gap] | None = None) -> list[Gap]:
    """Return only the gaps whose child classifies as ``in`` (the real backlog)."""
    return [g for g in (gaps or scan_gaps()) if classify(g.child)[0] == "in"]


def _grouped(gaps: list[Gap]) -> Iterator[tuple[str, list[Gap]]]:
    """Yield ``(classPkg, gaps)`` pairs, heaviest pkg first."""
    by_pkg: dict[str, list[Gap]] = {}
    for g in gaps:
        by_pkg.setdefault(_pkg(g.child), []).append(g)
    yield from sorted(by_pkg.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def format_report() -> str:
    """Return a human-readable, domain-grouped coverage report."""
    gaps = scan_gaps()
    buckets: dict[Bucket, list[Gap]] = {"in": [], "deferred": [], "out": []}
    reasons: dict[str, str] = {}
    for g in gaps:
        bucket, reason = classify(g.child)
        buckets[bucket].append(g)
        reasons[g.child] = reason

    lines: list[str] = ["# Coverage audit — curation gaps", ""]
    total = len(gaps)
    lines.append(
        f"{total} gaps · in-scope {len(buckets['in'])} · "
        f"deferred {len(buckets['deferred'])} · out {len(buckets['out'])}"
    )
    sections: tuple[tuple[str, Bucket], ...] = (
        ("## In-scope backlog (by domain)", "in"),
        ("## Deferred (backend-gated)", "deferred"),
        ("## Out of scope (with reason)", "out"),
    )
    for title, key in sections:
        lines += ["", title]
        for pkg, pkg_gaps in _grouped(buckets[key]):
            mk = sum(1 for g in pkg_gaps if g.kind == "maker")
            bd = len(pkg_gaps) - mk
            note = f"  — {reasons[pkg_gaps[0].child]}" if key != "in" else ""
            lines.append(f"- **{pkg}** (maker {mk} / bind {bd}){note}")
    return "\n".join(lines)


def snapshot() -> list[str]:
    """Return the sorted gap keys — the exact set the drift guard pins."""
    return [g.key for g in scan_gaps()]


def _main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the gap-key snapshot")
    args = parser.parse_args()
    if args.json:
        print(json.dumps(snapshot(), indent=2))
    else:
        print(format_report())


if __name__ == "__main__":
    _main()
