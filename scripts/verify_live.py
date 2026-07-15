"""Verify, object by object, that the walkthrough is really on the APIC.

Reads the fabric and confronts it with the designs the integration acts
declare — no hand-written expectations, no invented DNs: the acts are the
source of truth, this script only *observes*.

For every object a design declares it reports:

* **exists** — the DN is on the APIC;
* **attributes** — every declared attribute reads back with the declared value
  (through ``mo_diff``, the same comparator ``push(mode="plan")`` uses, so a
  normalisation the APIC applies shows up as drift, on purpose);
* **children** — every child object the design declares is itself verified,
  and the parent reports how many of them it accounted for;
* **faults** — the APIC's own verdict.  An object can be accepted and still be
  faulted: this is precisely what a manual GUI check hunts for, and it is the
  reason this script exists.

Usage (from the repo root, with the integration environment loaded):

    set -a; source .env; set +a
    uv run python scripts/verify_live.py                  # every act
    uv run python scripts/verify_live.py --act 5 --act 6  # only these
    uv run python scripts/verify_live.py --verbose        # list every object
    uv run python scripts/verify_live.py --report out.md  # write a markdown report

Exit code 0 when every declared object is present, unfaulted and matching;
1 otherwise — so it can gate a run the way the test suite does.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the acts live in tests/

from niwaki import Niwaki
from niwaki.design import Cursor
from niwaki.design._compiler import compile_ops
from niwaki.design._cursor import _load_class
from niwaki.design._resolver import resolve
from niwaki.exceptions import NotFoundError
from niwaki.utils.diff import mo_diff

# ── The designs under audit — imported from the acts, never re-typed ──────────


def _acts() -> dict[str, list[tuple[str, Callable[[], Cursor]]]]:
    """Act number → the named designs it declares."""
    from tests.integration import (
        test_01_fabric,
        test_02_access,
        test_03_tenant,
        test_04_l3out,
        test_05_observability,
        test_06_edge_and_management,
    )

    return {
        "1": [("fabric policies", test_01_fabric.fabric_design)],
        "2": [("access policies", test_02_access.access_design)],
        "3": [
            ("three-tier app", test_03_tenant.showcase_design),
            ("protocol policies", test_03_tenant.protocol_policies_design),
            ("EPG/ESG world", test_03_tenant.epg_world_design),
            ("contract world", test_03_tenant.contract_world_design),
            ("out-of-band contract", test_03_tenant.oob_contract_design),
        ],
        "4": [("L3Out", test_04_l3out.l3out_design)],
        "5": [("observability", test_05_observability.observability_design)],
        "6": [
            ("L2 edge", test_06_edge_and_management.edge_design),
            ("management", test_06_edge_and_management.management_design),
        ],
    }


# ── What a design expects ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Expected:
    """One object a design declares, and the children it declares under it."""

    dn: str
    aci_class: str
    payload: dict[str, str]  # wire attributes, as compiled for the POST
    children: tuple[str, ...]


def _parent_dn(dn: str) -> str:
    """Strip the last RN, ignoring the slashes inside bracketed naming values."""
    depth = 0
    for index in range(len(dn) - 1, -1, -1):
        char = dn[index]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
        elif char == "/" and depth == 0:
            return dn[:index]
    return ""


def _expectations(design: Cursor) -> list[_Expected]:
    """Compile a design into the objects it claims the APIC should hold."""
    root = design.design_node.root()
    declared: dict[str, tuple[str, dict[str, str]]] = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((aci_class, body),) = op.payload.items()
        declared[op.dn] = (aci_class, dict(body["attributes"]))

    children: dict[str, list[str]] = defaultdict(list)
    for dn in declared:
        parent = _parent_dn(dn)
        if parent in declared:
            children[parent].append(dn)

    return [
        _Expected(dn, aci_class, payload, tuple(sorted(children[dn])))
        for dn, (aci_class, payload) in sorted(declared.items())
    ]


# ── What the APIC holds ──────────────────────────────────────────────────────


@dataclass
class _Finding:
    """The verdict on one declared object."""

    dn: str
    aci_class: str
    child_count: int
    missing: bool = False
    drift: dict[str, tuple[object, object]] = field(default_factory=lambda: {})
    faults: list[str] = field(default_factory=lambda: [])

    @property
    def ok(self) -> bool:
        return not (self.missing or self.drift or self.faults)


# Faults we declare knowingly, with the reason.  Anything else fails the run.
_ACCEPTED_FAULTS = {
    # An out-of-band EPG belongs to a management zone only once a node is bound
    # to it.  Act 6 deliberately binds none — binding the simulator's own nodes
    # would rewrite their management addresses, and the point of the act is the
    # vocabulary (the EPG, its contract), not the deployment.
    ("uni/tn-mgmt/mgmtp-default/oob-niwaki-oob-epg", "F0523"),
}


def _faults_by_object(aci: Niwaki) -> dict[str, list[str]]:
    """Every active fault hanging under a configuration object, indexed by it.

    Faults are not returned by an ordinary subtree query — the APIC only serves
    them through ``rsp-subtree-include=faults`` or a class query.  One class
    query is cheaper than one enrichment per object, and a fault's DN is
    ``<faulted object>/fault-F1234``, so the parent DN names the culprit.

    Node-side faults (``topology/pod-1/node-101/...``) are left out on purpose:
    they report on the *deployed* object (drop rates, adjacency states), not on
    the configuration this script verifies.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for fault in aci.query("faultInst").fetch():
        data = fault.model_dump(by_alias=True)
        dn, severity = str(data.get("dn", "")), str(data.get("severity", ""))
        if not dn.startswith("uni/") or "/fault-" not in dn:
            continue
        if severity in ("cleared", "info"):
            continue
        culprit, code = _parent_dn(dn), str(data.get("code", "?"))
        if (culprit, code) in _ACCEPTED_FAULTS:
            continue
        found[culprit].append(f"{code} [{severity}] {str(data.get('descr', ''))[:100]}")
    return found


def _audit(aci: Niwaki, expected: list[_Expected], faults: dict[str, list[str]]) -> list[_Finding]:
    """Confront one design's expectations with the fabric."""
    findings: list[_Finding] = []
    for item in expected:
        cls = _load_class(item.aci_class)
        finding = _Finding(dn=item.dn, aci_class=item.aci_class, child_count=len(item.children))
        try:
            current = aci.node(item.dn, cls).read()
        except NotFoundError:
            finding.missing = True
            findings.append(finding)
            continue

        # The desired object, rebuilt from the very payload the push sent, then
        # compared with the same diff the plan mode uses: only what the design
        # declared is looked at.
        desired = cls.from_apic({item.aci_class: {"attributes": item.payload}})
        delta = mo_diff(desired, current, recurse_children=False, respect_fields_set=True)
        if delta is not None:
            for prop in delta.model_fields_set:
                if prop in ("children", "dn"):
                    continue
                finding.drift[prop] = (getattr(desired, prop), getattr(current, prop))

        finding.faults = faults.get(item.dn, [])
        findings.append(finding)
    return findings


# ── Reporting ────────────────────────────────────────────────────────────────


def _lines(name: str, findings: list[_Finding], *, verbose: bool) -> Iterator[str]:
    broken = [f for f in findings if not f.ok]
    children = sum(f.child_count for f in findings)
    mark = "OK  " if not broken else "FAIL"
    yield (
        f"[{mark}] {name}: {len(findings) - len(broken)}/{len(findings)} objects verified "
        f"({children} of them declared as children)"
    )
    for finding in findings:
        if finding.ok and not verbose:
            continue
        status = "  ok" if finding.ok else "  !!"
        kids = f", {finding.child_count} declared children" if finding.child_count else ""
        yield f"{status} {finding.dn}  ({finding.aci_class}{kids})"
        if finding.missing:
            yield "       MISSING on the APIC"
        for prop, (want, got) in sorted(finding.drift.items()):
            yield f"       drift {prop}: declared {want!r}, APIC holds {got!r}"
        for fault in finding.faults:
            yield f"       fault {fault}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--act", action="append", help="only these acts (1-6); repeatable")
    parser.add_argument("--verbose", action="store_true", help="list every object, not just faults")
    parser.add_argument("--report", help="also write the report to this markdown file")
    args = parser.parse_args()

    missing_env = [v for v in ("APIC_HOST", "APIC_USERNAME", "APIC_PASSWORD") if not os.getenv(v)]
    if missing_env:
        print(f"missing environment: {', '.join(missing_env)}", file=sys.stderr)
        return 2

    acts = _acts()
    selected = args.act or sorted(acts)
    if unknown := [a for a in selected if a not in acts]:
        print(f"unknown act(s): {', '.join(unknown)} (known: {', '.join(sorted(acts))})")
        return 2

    output: list[str] = []
    failed = False
    with Niwaki(
        os.environ["APIC_HOST"],
        os.environ["APIC_USERNAME"],
        os.environ["APIC_PASSWORD"],
        verify_ssl=False,
    ) as aci:
        faults = _faults_by_object(aci)
        for act in selected:
            output.append(f"\n══ Act {act} ══")
            for name, factory in acts[act]:
                findings = _audit(aci, _expectations(factory()), faults)
                failed |= any(not f.ok for f in findings)
                output.extend(_lines(name, findings, verbose=args.verbose))

    text = "\n".join(output)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(f"# Live verification\n\n```text\n{text}\n```\n")
        print(f"\nreport written to {args.report}")

    print("\n" + ("SOME OBJECTS DIVERGE — see above" if failed else "EVERY DECLARED OBJECT IS OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
