"""Propose vocabulary candidates for a curation wave.

Usage (from repo root):
    uv run python -m niwaki._codegen.propose_vocabulary l3extOut --wave wave-1-l3out
    uv run python -m niwaki._codegen.propose_vocabulary fvESg vzAny --max-depth 3

The assisted-curation half of the vocabulary workflow: given one or more
subtree roots, this tool walks the schema containment, derives maker names,
auto-proposes ``bind`` aliases from REFERENCE_MAP and contract verbs from the
``Rs*Prov``/``Rs*Cons`` children, and emits a candidate YAML block shaped
exactly like ``domain/vocabulary.yaml`` — ready to review and merge by hand.

The output is a **proposal, never an input**: it is written under
``data/candidates/`` (gitignored) and only the human-merged entries in
``vocabulary.yaml`` feed the generators.  Every doubtful line carries a
``# REVIEW:`` comment so a wave review is a scan, not an audit:

- ``collision-renamed`` — siblings shared a label; the name came out of the
  disambiguation cascade and deserves a read.
- ``long-name`` — over 40 characters or 5+ underscores; consider a curated
  shorthand (``external_epg`` instead of the full schema label).
- ``deep`` — more than 4 maker levels below the wave root.
- ``abstract-target`` — the bind alias points at an abstract class; decide
  abstract-vs-concrete explicitly.

Maker names come **verbatim from CHILD_MAP** (the read-navigation jargon):
the curated maker name must agree with the facade jargon
(``test_core_yaml.test_maker_name_matches_facade_jargon``), so proposing
anything else would only manufacture whitelist entries.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import cache
from pathlib import Path
from typing import Any

from niwaki._codegen.generate_domain import SCHEMA_DIR, _load_schemas, _normalise

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
CANDIDATES_DIR = _REPO_ROOT / "data" / "candidates"

# Families the enrichment deliberately leaves out (user decision 2026-07-12):
# Cloud APIC and L4-L7 service graphs.
EXCLUDED_PKGS: frozenset[str] = frozenset({"cloud", "vns"})

# Cross-cutting carriers present under almost every parent — they would bury
# the real proposals and stay `.mo()` territory.
NOISY_CLASSES: frozenset[str] = frozenset(
    {
        "tagAnnotation",
        "tagAliasInst",
        "tagTag",
        "tagInst",
        "tagExtMngdInst",
        "aaaRbacAnnotation",
        "aaaDomainRef",
    }
)

# Review-flag thresholds.
_LONG_NAME_CHARS = 40
_LONG_NAME_WORDS = 5
_DEEP_LEVELS = 4


@cache
def _schemas() -> dict[str, dict[str, Any]]:
    """Configurable concrete classes (cached — the schema dir is immutable)."""
    return _load_schemas()


@cache
def _load_labels() -> dict[str, str]:
    """Map every schema class (normalised) to its label — targets included.

    ``_load_schemas`` keeps only configurable concrete classes; bind targets
    can be abstract or read-only, so alias naming needs the full label set.
    """
    labels: dict[str, str] = {}
    for f in SCHEMA_DIR.glob("*.json"):
        for _key, entry in json.loads(f.read_text()).items():
            labels[_normalise(_key)] = entry.get("label", "")
    return labels


def _reverse_child_map(parent: str) -> dict[str, str]:
    """CHILD_MAP row for *parent*, inverted: ``{child_class: method_name}``."""
    from niwaki.domain._child_map import CHILD_MAP

    return {child: method for method, child in CHILD_MAP.get(parent, {}).items()}


class _Wave:
    """One proposal run: the walked subtree and its derived tables."""

    def __init__(
        self, roots: list[str], max_depth: int, allow: frozenset[str] = frozenset()
    ) -> None:
        self.roots = roots
        self.max_depth = max_depth
        self.allow = allow
        self.classes = _schemas()
        self.labels = _load_labels()
        self.makers: dict[str, dict[str, tuple[str, list[str]]]] = {}
        self.binds: dict[str, dict[str, tuple[str, list[str]]]] = {}
        self.verbs: dict[str, dict[str, dict[str, str]]] = {}
        self.skipped_curated = 0

        self._children_of: dict[str, list[str]] = {}
        for cls, info in self.classes.items():
            for parent in info["containedBy"]:
                self._children_of.setdefault(parent, []).append(cls)

    # ── Walk ──────────────────────────────────────────────────────────────────

    def build(self) -> None:
        """Walk each root subtree and fill the proposal tables."""
        for root in self.roots:
            if root not in self.classes:
                sys.exit(f"propose_vocabulary: unknown or non-configurable class {root!r}")
            self._anchor_root(root)
            self._propose_binds(root, depth=0)
            self._propose_verbs(root)
            self._walk(root, depth=0, lineage=(root,))

    def _anchor_root(self, root: str) -> None:
        """Propose the parent→root maker line under already-curated parents.

        A wave root is only reachable in the DSL if some curated position can
        make it — when its schema parent is itself a curated class (fvTenant
        for the tenant protocol policies), the anchoring line is exactly what
        the wave needs to add.
        """
        from niwaki.design._cursor import _tables

        curated_classes = {"polUni"} | {
            child for table in _tables().makers.values() for child in table.values()
        }
        for parent in self.classes[root]["containedBy"]:
            if parent not in curated_classes:
                continue
            if root in set(_tables().makers.get(parent, {}).values()):
                continue  # already anchored
            name = _reverse_child_map(parent).get(root, "")
            if name:
                self.makers.setdefault(parent, {})[name] = (root, self._maker_flags(name, 0))

    def _eligible(self, child: str) -> bool:
        info = self.classes[child]
        if info["flavor"] != "":
            return False  # Rs classes become binds, not makers
        if child in self.allow:
            return True  # explicit override of the family/noise denylists
        return child not in NOISY_CLASSES and info["classPkg"] not in EXCLUDED_PKGS

    def _walk(self, parent: str, depth: int, lineage: tuple[str, ...]) -> None:
        from niwaki.design._cursor import _tables

        if depth >= self.max_depth:
            return
        curated = set(_tables().makers.get(parent, {}).values())
        names = _reverse_child_map(parent)
        for child in sorted(self._children_of.get(parent, ())):
            if not self._eligible(child) or child in lineage:  # containment cycles exist
                continue
            if child in curated:
                self.skipped_curated += 1
            else:
                name = names.get(child, "")
                if not name:
                    continue  # not in CHILD_MAP → not navigable, leave to .mo()
                flags = self._maker_flags(name, depth)
                self.makers.setdefault(parent, {})[name] = (child, flags)
                self._propose_binds(child, depth + 1)
                self._propose_verbs(child)
            self._walk(child, depth + 1, (*lineage, child))

    def _maker_flags(self, name: str, depth: int) -> list[str]:
        flags: list[str] = []
        if len(name) > _LONG_NAME_CHARS or name.count("_") >= _LONG_NAME_WORDS:
            flags.append("long-name")
        if depth > _DEEP_LEVELS:
            flags.append("deep")
        return flags

    # ── Binds & verbs ─────────────────────────────────────────────────────────

    def _propose_binds(self, owner: str, depth: int) -> None:
        from niwaki.design._cursor import _tables
        from niwaki.domain._child_map import REFERENCE_MAP, TARGET_SUBCLASSES

        curated_targets = set(_tables().binds.get(owner, {}).values())
        verb_rs = {spec["rs"] for spec in _tables().verbs.get(owner, {}).values()}
        for target, (rs, _flavor) in sorted(REFERENCE_MAP.get(owner, {}).items()):
            if (
                target in curated_targets
                or rs in verb_rs
                or rs.startswith(("fvRsProv", "fvRsCons"))
            ):
                continue
            label = self.labels.get(target, "")
            alias = _alias_name(label, target)
            flags: list[str] = []
            if target in TARGET_SUBCLASSES:
                flags.append("abstract-target")
            if len(alias) > _LONG_NAME_CHARS or alias.count("_") >= _LONG_NAME_WORDS:
                flags.append("long-name")
            self.binds.setdefault(owner, {})[alias] = (target, flags)

    def _propose_verbs(self, owner: str) -> None:
        from niwaki.design._cursor import _tables

        if _tables().verbs.get(owner):
            return
        found: dict[str, dict[str, str]] = {}
        for child in self._children_of.get(owner, ()):
            info = self.classes[child]
            if info["flavor"] != "name" or not info["to_mo"]:
                continue
            if info["className"].endswith("Prov"):
                found["provide"] = {"rs": child, "target": info["to_mo"]}
            elif info["className"].endswith("Cons"):
                found["consume"] = {"rs": child, "target": info["to_mo"]}
        if len(found) == 2:  # propose the pair or nothing — one-sided is suspect
            self.verbs[owner] = found

    # ── Output ────────────────────────────────────────────────────────────────

    def render(self) -> str:
        """Render the candidate YAML block (vocabulary.yaml section shapes)."""
        n_makers = sum(len(t) for t in self.makers.values())
        n_binds = sum(len(t) for t in self.binds.values())
        n_review = sum(
            1
            for table in (*self.makers.values(), *self.binds.values())
            for _, flags in table.values()
            if flags
        )
        roots = ", ".join(self.roots)
        lines = [
            f"# Vocabulary candidates — roots: {roots} (max depth {self.max_depth})",
            "# Generated by propose_vocabulary — REVIEW then merge by hand into",
            "# src/niwaki/domain/vocabulary.yaml.  This file is never read by code.",
            f"# {n_makers} makers, {n_binds} bind aliases, {len(self.verbs)} verb pairs"
            f" — {n_review} lines flagged for review;"
            f" {self.skipped_curated} already-curated positions skipped.",
            "",
            "makers:",
        ]
        for parent in sorted(self.makers):
            lines.append(f"  {parent}:")
            for name, (child, flags) in sorted(self.makers[parent].items()):
                comment = f"  # REVIEW: {', '.join(flags)}" if flags else ""
                lines.append(f"    {name}: {child}{comment}")
        lines.append("")
        lines.append("binds:")
        for owner in sorted(self.binds):
            lines.append(f"  {owner}:")
            for alias, (target, flags) in sorted(self.binds[owner].items()):
                comment = f"  # REVIEW: {', '.join(flags)}" if flags else ""
                lines.append(f"    {alias}: {target}{comment}")
        lines.append("")
        lines.append("verbs:")
        for owner in sorted(self.verbs):
            lines.append(f"  {owner}:")
            for verb, spec in sorted(self.verbs[owner].items()):
                lines.append(f"    {verb}: {{rs: {spec['rs']}, target: {spec['target']}}}")
        lines.append("")
        return "\n".join(lines)

    def report(self) -> str:
        """One-paragraph stdout summary."""
        n_makers = sum(len(t) for t in self.makers.values())
        n_binds = sum(len(t) for t in self.binds.values())
        flagged = sum(
            1
            for table in (*self.makers.values(), *self.binds.values())
            for _, flags in table.values()
            if flags
        )
        return (
            f"propose_vocabulary: {n_makers} makers under {len(self.makers)} parents, "
            f"{n_binds} bind aliases, {len(self.verbs)} verb pairs; "
            f"{flagged} flagged for review, {self.skipped_curated} already curated."
        )


def _alias_name(label: str, aci_class: str) -> str:
    """Bind-alias spelling for a target class (label first, class fallback)."""
    from niwaki._codegen.generate_domain import _derive_name, _normalise_method

    return _normalise_method(_derive_name(aci_class, label, ""))


def propose(roots: list[str], max_depth: int = 6, allow: frozenset[str] = frozenset()) -> _Wave:
    """Build the proposal for *roots* (public entry point, used by tests).

    Args:
        roots: Subtree root ACI classes (e.g. ``["l3extOut"]``).
        max_depth: Maker levels to walk below each root.
        allow: Classes explicitly exempted from the family/noise denylists
            (e.g. the standalone L4-L7 policy classes inside the excluded
            ``vns`` package).

    Returns:
        The populated :class:`_Wave` (call ``render()`` for the YAML).
    """
    wave = _Wave(roots, max_depth, allow)
    wave.build()
    return wave


def main() -> None:
    """CLI entry point — write the candidate file and print the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="subtree root ACI classes (e.g. l3extOut)")
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--wave", default=None, help="candidate file name (data/candidates/)")
    parser.add_argument(
        "--allow",
        nargs="*",
        default=[],
        help="classes exempted from the family/noise denylists",
    )
    args = parser.parse_args()

    wave = propose(args.roots, args.max_depth, frozenset(args.allow))
    output = wave.render()
    if args.wave:
        CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
        path = CANDIDATES_DIR / f"{args.wave}.yaml"
        path.write_text(output, encoding="utf-8")
        print(f"propose_vocabulary: wrote {path}")
    else:
        print(output)
    print(wave.report(), file=sys.stderr)


if __name__ == "__main__":
    main()
