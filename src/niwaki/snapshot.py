"""Deterministic, git-diffable snapshots of a fabric's configuration.

A snapshot is a **fact**: every instance of every exportable class under a
scope DN, read through the sharded, paginated bulk reader, normalised by the
catalogue's own metadata, and serialised so that the same fabric state always
produces the same bytes.  Two snapshots of an unchanged fabric ``git diff``
empty; a config change diffs as exactly that change.

Three catalogue-driven decisions, none of them a hand-maintained list:

- **Which objects**: classes the schema marks ``isExportable`` — Cisco's own
  definition of what a configuration export contains (measured on 6.0(9c):
  exportable is a strict subset of configurable; the difference is runtime
  state such as login sessions, which a backup must not carry).  The curated
  DSL vocabulary plays no role here: an object the DSL cannot express is
  still configuration, and ignoring it would make the backup lie.
- **Which properties**: those the schema marks ``isConfigurable`` — the
  operational halo (timestamps, status, computed backpointers) falls away
  data-driven, never through a denylist.
- **What never ships**: the curated secret policy.  Values
  the schema flags ``secure`` never read back from an APIC in the first
  place; the curated positions the flag misses are redacted to
  ``"<redacted>"``; and an object whose **DN** carries a secret (an SNMP
  community string names its own object) cannot be redacted in place — it is
  reported in :attr:`Snapshot.warnings` so the caller decides, instead of
  publishing a secret to git silently.

The serialised form is **wire-format** (APIC class and property names), so a
snapshot survives any renaming of the SDK's readable surface unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from niwaki._dn import parent_dn, rn_of
from niwaki._read import shard_classes
from niwaki._secrets import SECRET_VALUE_POSITIONS, secret_dn_classes
from niwaki.exceptions._query import UnknownClassError
from niwaki.query import _catalog
from niwaki.types import JsonObject, JsonValue

if TYPE_CHECKING:
    from niwaki.facade import Niwaki
    from niwaki.query._catalog import Catalog

#: The stable sentinel written in place of a curated secret value.
REDACTED = "<redacted>"


def _snapshot_universe() -> tuple[frozenset[str], frozenset[str]]:
    """(classes to request, classes to keep) — both from the shipped catalogue.

    Kept classes are the concrete exportable ones.  Requested classes add
    the **transitive** containment ancestors of every kept class: a class
    read with no instances under the scope costs nothing, and reading the
    whole ancestor chain keeps the tree connected so a kept object several
    non-exportable containers deep never drops as an orphan (one-level closure
    would drop it — the parent attaches but the grandparent is unread).
    """
    from niwaki.domain._child_map import CHILD_MAP

    reader = _catalog.catalog()
    # FaultCurrent is excluded from ``keep``: faultInst is the one exportable
    # class that is runtime state, not configuration — a fault raising or
    # clearing must not read as config drift (measured: a new BD's unformed
    # relation carried its fault straight into the capture).
    keep = reader._exportable_classes()
    concrete = reader._concrete_classes()

    # Invert CHILD_MAP to {child: {parents}} once, then walk ancestors upward.
    parents_of: dict[str, set[str]] = {}
    for parent, children in CHILD_MAP.items():
        for child in children.values():
            parents_of.setdefault(child, set()).add(parent)

    ancestors: set[str] = set()
    frontier = set(keep)
    while frontier:
        nxt: set[str] = set()
        for cls in frontier:
            for parent in parents_of.get(cls, ()):
                if parent not in ancestors:
                    ancestors.add(parent)
                    nxt.add(parent)
        frontier = nxt
    return frozenset(keep | (ancestors & concrete)), keep


@dataclass(frozen=True)
class Snapshot:
    """One deterministic capture of a scope's configuration.

    Attributes:
        scope: The DN the snapshot was rooted at (e.g. ``"uni"``,
            ``"uni/tn-prod"``).
        tree: The captured configuration as nested plain dicts, wire-format::

                {"class": "fvTenant", "rn": "tn-prod",
                 "attributes": {"name": "prod", ...},
                 "children": [...]}

            Children are sorted by ``(class, rn)`` and attributes by name —
            the determinism lives in the data, not in the serialiser.
        coverage: Instance count per class — the **interpretation** beside
            the fact: what the capture actually contains, at a glance.
        warnings: Human-readable notices the caller must not ignore, one per
            object whose DN itself carries a secret (an SNMP community
            profile is named by its community string).
    """

    scope: str
    tree: JsonObject | None
    coverage: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Serialise deterministically — same state, same bytes.

        One attribute per line, sorted keys, stable child order: the format
        is chosen for ``git diff``, not for compactness.
        """
        payload = {
            "niwaki_snapshot": {
                "scope": self.scope,
                "coverage": dict(sorted(self.coverage.items())),
                "warnings": list(self.warnings),
                "tree": self.tree,
            }
        }
        return json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        """Rehydrate a snapshot serialised by :meth:`to_json`.

        Raises:
            KeyError: The text is not a niwaki snapshot document.
        """
        body = json.loads(text)["niwaki_snapshot"]
        return cls(
            scope=body["scope"],
            tree=body["tree"],
            coverage=dict(body["coverage"]),
            warnings=tuple(body["warnings"]),
        )


def take(aci: Niwaki, scope: str = "uni") -> Snapshot:
    """Capture the configuration under *scope* into a :class:`Snapshot`.

    Reads every exportable class under the scope through the sharded,
    paginated bulk reader (one request in the common case, several when the
    class list or the result set demands it), then normalises:

    - non-configurable properties are dropped (operational halo);
    - curated secret values are replaced by :data:`niwaki.snapshot.REDACTED`;
    - objects whose DN carries a secret are collected into
      :attr:`Snapshot.warnings`.

    Args:
        aci: A connected :class:`niwaki.Niwaki` client (observation only —
            a snapshot never writes).
        scope: The subtree to capture.  ``"uni"`` for the whole configuration,
            ``"uni/tn-<name>"`` for one tenant, any config DN for narrower.

    Returns:
        The :class:`Snapshot`.  ``tree`` is ``None`` when nothing exists at
        *scope*.

    Example::

        snap = snapshot.take(aci, "uni/tn-prod")
        Path("tn-prod.json").write_text(snap.to_json())
    """
    session = aci._sync_session
    request, keep = _snapshot_universe()

    # The scope root is read by a plain, unfiltered self GET.  Class-filtered
    # subtree queries never return certain special objects — measured live:
    # ``target-subtree-class=polUni`` on ``/api/mo/uni.json`` answers zero
    # items even though the object exists — and the root must be present for
    # the tree to assemble at all.  One extra request, correct for any scope.
    items: list[JsonObject] = list(session.get(f"/api/mo/{scope}.json"))
    if not items:
        return Snapshot(scope=scope, tree=None)
    for shard in shard_classes(request):
        items.extend(
            session._get_all_pages(
                f"/api/mo/{scope}.json",
                {"query-target": "subtree", "target-subtree-class": shard},
                page_size=500,
            )
        )
    return _assemble(scope, items, keep)


def _normalise_attrs(
    reader: Catalog, cls_name: str, attrs: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Drop the operational halo, redact secrets — sorted, deterministic.

    A class the shipped catalogue does not know (the scope root can be
    anything the APIC returns) keeps its attributes verbatim: there is no
    flag table to filter by, and dropping them silently would be worse than
    passing them through.
    """
    try:
        flags = reader.prop_flags(cls_name)
    except UnknownClassError:
        return dict(sorted(attrs.items()))
    kept: dict[str, JsonValue] = {}
    for wire, value in attrs.items():
        prop = flags.get(wire)
        if prop is None or not prop.is_configurable:
            continue
        if prop.secure or (cls_name, wire) in SECRET_VALUE_POSITIONS:
            value = REDACTED
        kept[wire] = value
    return dict(sorted(kept.items()))


@dataclass(slots=True)
class _TreeNode:
    """Typed view of one snapshot tree node — the JSON shape, proven.

    :attr:`Snapshot.tree` is plain JSON (it is the serialised artefact); the
    code that builds or walks it works on this view instead, so every field
    has its real type.  :func:`_tree_node` parses the JSON form (fail-loud on
    a malformed document), :meth:`to_json` renders it back.

    Attributes:
        aci_class: Wire class name (the JSON ``"class"`` key).
        rn: Relative name.
        attributes: Wire attributes, wire string values.
        children: Child nodes.
    """

    aci_class: str
    rn: str
    attributes: dict[str, JsonValue]
    children: list[_TreeNode] = field(default_factory=list)

    def to_json(self) -> JsonObject:
        """Render the node (and its subtree) as the snapshot JSON shape."""
        attributes: JsonObject = dict(self.attributes)
        return {
            "class": self.aci_class,
            "rn": self.rn,
            "attributes": attributes,
            "children": [child.to_json() for child in self.children],
        }


type _OnMalformed = Callable[[str, str], None]
"""Receives ``(dn, detail)`` for a malformed node that is then skipped."""


def _malformed(dn: str, detail: str, on_malformed: _OnMalformed | None) -> None:
    """Report one malformed node: raise, or hand it to the collector."""
    if on_malformed is None:
        raise ValueError(f"malformed snapshot node at {dn}: {detail}")
    on_malformed(dn, detail)


def _tree_node(
    node: JsonValue, dn: str, on_malformed: _OnMalformed | None = None
) -> _TreeNode | None:
    """Parse one snapshot tree node (and its subtree) from its JSON form.

    Attribute values are kept exactly as written — ``take()`` writes wire
    strings, a hand-built snapshot may hold any JSON scalar.  The *structure*
    is checked: an object with a string ``class`` and ``rn``, an object
    ``attributes`` and a list ``children``.

    Args:
        node: A ``{"class", "rn", "attributes", "children"}`` object.
        dn: The node's DN, for the report (its parent's DN for a child whose
            own ``rn`` is unreadable).
        on_malformed: ``None`` to raise on the first malformed node; else a
            callback receiving ``(dn, detail)`` per malformed node, which is
            then skipped (with its subtree) while the rest still parses.

    Returns:
        The typed view, or ``None`` when *node* itself is malformed and
        *on_malformed* collected it.

    Raises:
        ValueError: A node is malformed and *on_malformed* is ``None``.
    """
    if not isinstance(node, dict):
        _malformed(dn, f"a node is {type(node).__name__}, not an object", on_malformed)
        return None
    aci_class, rn = node.get("class"), node.get("rn")
    if not isinstance(aci_class, str) or not isinstance(rn, str):
        _malformed(dn, "a node's 'class' and 'rn' must be strings", on_malformed)
        return None
    attributes, children = node.get("attributes"), node.get("children")
    if not isinstance(attributes, dict):
        _malformed(dn, f"{aci_class} {rn!r} has no 'attributes' object", on_malformed)
        return None
    if not isinstance(children, list):
        _malformed(dn, f"{aci_class} {rn!r} has no 'children' list", on_malformed)
        return None
    parsed = _TreeNode(aci_class, rn, dict(attributes))
    for child in children:
        child_rn = child.get("rn") if isinstance(child, dict) else None
        child_dn = f"{dn}/{child_rn}" if isinstance(child_rn, str) else dn
        sub = _tree_node(child, child_dn, on_malformed)
        if sub is not None:
            parsed.children.append(sub)
    return parsed


def _parse_tree(snapshot: Snapshot) -> _TreeNode | None:
    """The typed view of *snapshot*'s tree, or ``None`` for an empty capture.

    Raises:
        ValueError: The tree is structurally malformed.
    """
    if snapshot.tree is None:
        return None
    return _tree_node(snapshot.tree, snapshot.scope)


def _assemble(scope: str, items: list[JsonObject], keep: frozenset[str]) -> Snapshot:
    """Pure second half of :func:`take`: normalise, redact, attach, prune, count."""
    reader = _catalog.catalog()

    nodes: dict[str, _TreeNode] = {}
    for item in items:
        (cls_name,) = item.keys()
        body = item[cls_name]
        raw_attrs = body.get("attributes", {}) if isinstance(body, dict) else None
        if not isinstance(raw_attrs, dict):
            raise TypeError(f"{cls_name} envelope carries no attributes object")
        attrs = dict(raw_attrs)
        dn = attrs.pop("dn", None)
        if isinstance(dn, str) and dn:  # a paginated read can echo a boundary DN
            nodes[dn] = _node(cls_name, dn, reader, attrs)

    root = nodes.get(scope)
    if root is None:
        return Snapshot(scope=scope, tree=None)

    for dn, node in nodes.items():
        if dn == scope:
            continue
        parent = nodes.get(parent_dn(dn) or "")
        if parent is not None:
            parent.children.append(node)

    def _sort(node: _TreeNode) -> None:
        node.children.sort(key=lambda child: (child.aci_class, child.rn))
        for child in node.children:
            _sort(child)

    def _prune(node: _TreeNode) -> bool:
        """Keep a node when it is exportable or shelters an exportable descendant."""
        node.children = [child for child in node.children if _prune(child)]
        return node.aci_class in keep or bool(node.children)

    root.children = [child for child in root.children if _prune(child)]
    _sort(root)

    # Coverage and warnings are read off the FINAL tree — the fact — so they
    # never count or name an object the capture does not contain.  A login
    # session read only as a tag-parent is pruned here, and so never warns:
    # its token-hash DN stays out of the artifact, and the bytes stay stable
    # across runs whose only difference is who was logged in.
    dn_secret_classes = secret_dn_classes()
    coverage: dict[str, int] = {}
    warnings: list[str] = []

    def _account(node: _TreeNode, dn: str) -> None:
        if node.aci_class in keep or dn == scope:
            coverage[node.aci_class] = coverage.get(node.aci_class, 0) + 1
        if node.aci_class in dn_secret_classes:
            warnings.append(
                f"{dn}: the DN of a {node.aci_class} carries a secret in its naming "
                "property; it cannot be redacted in place"
            )
        for child in node.children:
            _account(child, f"{dn}/{child.rn}")

    _account(root, scope)
    return Snapshot(
        scope=scope, tree=root.to_json(), coverage=coverage, warnings=tuple(sorted(warnings))
    )


def _node(cls_name: str, dn: str, reader: Catalog, attrs: dict[str, JsonValue]) -> _TreeNode:
    return _TreeNode(cls_name, rn_of(dn), _normalise_attrs(reader, cls_name, attrs))


# ── Diff — two snapshots, one verdict (brick: drift detection) ────────────────


@dataclass(frozen=True)
class SnapshotDiff:
    """The structural difference between two snapshots.

    Attributes:
        added: DNs present in *b* but not in *a*, sorted.
        removed: DNs present in *a* but not in *b*, sorted.
        changed: Per-DN attribute deltas, ``{dn: {wire: (a_value, b_value)}}``
            — only attributes that differ, values as written (wire strings
            from :func:`take`, any JSON scalar in a hand-built snapshot); an
            attribute present on one side only appears with ``None`` on the
            other, so a ``null`` value reads like an absent one.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: dict[str, dict[str, tuple[JsonValue, JsonValue]]]

    @property
    def has_changes(self) -> bool:
        """``True`` when the two snapshots describe different configuration."""
        return bool(self.added or self.removed or self.changed)


def _flatten(snapshot: Snapshot) -> dict[str, dict[str, JsonValue]]:
    """``{dn: attributes}`` for every node of a snapshot's tree."""
    flat: dict[str, dict[str, JsonValue]] = {}

    def _walk(node: _TreeNode, parent: str) -> None:
        dn = node.rn if not parent else f"{parent}/{node.rn}"
        flat[dn] = node.attributes
        for child in node.children:
            _walk(child, dn)

    tree = _parse_tree(snapshot)
    if tree is not None:
        # The root's rn is not part of its own scope DN reconstruction: use
        # the scope verbatim so tenant-scoped and uni-scoped snapshots of the
        # same subtree flatten to comparable DNs.
        flat[snapshot.scope] = tree.attributes
        for child in tree.children:
            _walk(child, snapshot.scope)
    return flat


def diff(a: Snapshot, b: Snapshot) -> SnapshotDiff:
    """Compare two snapshots structurally — the drift detector.

    Works on any two snapshots of the same scope: two moments of one fabric
    (config drift), or the same scope on two fabrics (divergence between a
    reference fabric and a replica).

    Args:
        a: The reference snapshot ("before").
        b: The other snapshot ("after").

    Returns:
        A :class:`SnapshotDiff`.  Empty (``has_changes`` false) when the two
        captures describe identical configuration.

    Raises:
        ValueError: A snapshot tree is structurally malformed (a node that
            is not an object, a missing or non-string ``class``/``rn``,
            ``attributes`` not an object, ``children`` not a list) — or the
            snapshots cover different scopes — comparing a tenant
            to a whole fabric is a mistake, not a diff.

    Example::

        before = snapshot.take(aci, "uni/tn-prod")
        ...
        after = snapshot.take(aci, "uni/tn-prod")
        delta = snapshot.diff(before, after)
        for dn in delta.added:
            print("new object:", dn)
    """
    if a.scope != b.scope:
        raise ValueError(f"snapshot scopes differ: {a.scope!r} vs {b.scope!r}")
    flat_a, flat_b = _flatten(a), _flatten(b)

    added = tuple(sorted(set(flat_b) - set(flat_a)))
    removed = tuple(sorted(set(flat_a) - set(flat_b)))
    changed: dict[str, dict[str, tuple[JsonValue, JsonValue]]] = {}
    for dn in sorted(set(flat_a) & set(flat_b)):
        attrs_a, attrs_b = flat_a[dn], flat_b[dn]
        delta = {
            wire: (attrs_a.get(wire), attrs_b.get(wire))
            for wire in sorted(set(attrs_a) | set(attrs_b))
            if attrs_a.get(wire) != attrs_b.get(wire)
        }
        if delta:
            changed[dn] = delta
    return SnapshotDiff(added=added, removed=removed, changed=changed)
