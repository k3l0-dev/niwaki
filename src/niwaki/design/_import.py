"""Reverse import — rebuild a design from a :class:`~niwaki.snapshot.Snapshot`.

:func:`to_design` walks a snapshot tree (wire-format facts captured by
:func:`niwaki.snapshot.take`) and re-expresses it in the design DSL — the
inverse of ``push``.  The importer prefers the curated vocabulary and falls
back to the wire-name escape hatches, so the resulting design compiles to the
same wire payload the snapshot describes:

1. **Verbs** — a relationship class curated as a verb on its parent
   (``fvRsProv`` under an EPG) becomes ``provide(...)`` / ``consume(...)`` …
2. **Makers** — a child class with a curated maker under its parent becomes
   that maker call; naming values are recovered from the RN, remaining
   attributes go through ``set()`` with full model validation.
3. **Binds** — a relationship class reachable through a curated ``bind()``
   alias becomes ``bind(alias=name)`` when its target is in the snapshot,
   ``bind_dn(alias=dn)`` when a DN-flavored target is not; attributes on the
   relationship itself ride a :func:`~niwaki.design.ref`.
4. **Raw** — everything else goes through :meth:`~niwaki.design.Cursor.raw`
   (the typed route for generated classes, the catalogue route otherwise).
   The fallback is never silent and never wrong: it emits the exact wire
   attributes the snapshot holds.

On two points the snapshot outranks the SDK's own derived tables, because
the fabric is the authority on itself (all measured live on one sim):

- **Containment**: an edge the ``CHILD_MAP``/``_contains`` tables lack but
  the snapshot exhibits (``commTelnet`` under ``commPol``,
  ``uiSettingsCont`` under ``polUni``) imports anyway — the object exists
  under exactly that parent.
- **Properties the catalogue knows but the generated model lacks** (an
  extraction gap: ``commHttps.dhParam``) ride the wire channel by default,
  validated against the catalogue.

Fidelity is the invariant, idiomaticity is best-effort: whenever a curated
inversion cannot be made *provably* equivalent (an ambiguous alias, a
dangling target, a relationship carrying children), the importer drops to the
next rung of the ladder rather than guessing.

The policies, rooted in the 2026-08-09 scoping and in one measured fact —
**a live snapshot carries every configurable property at its current wire
value**, unset markers included (``annotation=""`` on nearly every object,
``vmac="not-applicable"`` on every BD, ``vrfIndex="0"`` on every VRF) — so
the importer normalises values before the strict models see them:

- **Empty strings are the wire spelling of "not configured"** and are
  dropped: the design layer cannot emit ``""`` by contract (``to_apic``
  skips it so a push never blanks a value), so dropping is the identity
  transform for both push and plan.
- **A value the model refuses that equals the property's schema default**
  (the shipped catalogue carries defaults) is an unset marker Cisco spells
  outside its own value space — ``not-applicable`` on a MAC, ``0`` below an
  ``1..N`` range — and is dropped the same way.
- **Any other value the model refuses is escaped to the wire channel**
  (``raw_set``): the APIC served it, so the wire is authoritative and the
  annotation is the thing that lies.  This subsumes the scoped decision on
  unknown enum members — always escaped, no opt-in.  A value that is
  *genuinely* wrong still fails loudly at the only authority that can tell
  (the controller, at push) and reads as drift in ``mode="plan"``.
- **Unknown class/property** (a snapshot from a newer firmware): collected
  into one :class:`~niwaki.exceptions.SnapshotImportError` — or, with
  ``on_unknown="raw"``, carried verbatim on the wire-attribute channel.
- **Redacted secrets** (the :data:`~niwaki.snapshot.REDACTED` sentinel):
  collected and raised — a design pushing the sentinel literally would be
  wrong — or dropped with ``redacted="skip"``.

Nothing else is ever elided: every attribute the snapshot carries reaches
the design (the snapshot already dropped the operational halo at capture),
and every drop above is the reproduction of "unset", never of a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import pydantic

from niwaki._dn import naming_values, split_dn
from niwaki.design._cursor import (
    Cursor,
    _attach,
    _load_class,
    _make_child,
    _maker_allowed,
    _tables,
)
from niwaki.design._node import DesignNode, RawDesignNode, Ref
from niwaki.exceptions._design import (
    DesignError,
    DuplicateDeclarationError,
    SnapshotImportError,
)

if TYPE_CHECKING:
    from niwaki.models.base import ManagedObject
    from niwaki.snapshot import Snapshot

_ProblemKind = Literal[
    "unknown-class", "unknown-property", "redacted-value", "invalid-value", "structure"
]

_SnapNode = dict[str, Any]


@dataclass(frozen=True)
class ImportProblem:
    """One snapshot item :func:`to_design` cannot import.

    Attributes:
        dn: DN of the offending object (reconstructed from the snapshot tree).
        kind: Problem family — ``"unknown-class"``, ``"unknown-property"``,
            ``"redacted-value"``, ``"invalid-value"`` or ``"structure"``.
        detail: Human-readable description naming the class/property/value.
    """

    dn: str
    kind: _ProblemKind
    detail: str


# ── Inversion tables (lazy, module-cached — pure reads of curated data) ───────


@cache
def _maker_inversion() -> dict[tuple[str, str], str]:
    """``{(parent_class, child_class): maker_label}`` — the makers, inverted.

    Measured on the full vocabulary: the 845 curated makers invert with zero
    collisions, so this is a function, not a relation.  Guarded by a unit
    test; should curation ever introduce a second maker for the same
    ``(parent, child)`` pair, the first one in vocabulary order wins here and
    the test forces a conscious decision.
    """
    inv: dict[tuple[str, str], str] = {}
    for parent, table in _tables().makers.items():
        for label, child in table.items():
            inv.setdefault((parent, child), label)
    return inv


@cache
def _verb_inversion() -> dict[tuple[str, str], tuple[str, str]]:
    """``{(owner_class, rs_class): (verb, target_class)}`` — the verbs, inverted.

    Unique by construction (measured: no two verbs on one owner share an Rs
    class — that is the very reason verbs exist).
    """
    inv: dict[tuple[str, str], tuple[str, str]] = {}
    for owner, table in _tables().verbs.items():
        for verb, spec in table.items():
            inv[(owner, spec["rs"])] = (verb, spec["target"])
    return inv


@dataclass(frozen=True)
class _BindAlias:
    """One curated ``bind()`` alias, seen from the Rs class it resolves to.

    Attributes:
        alias: The vocabulary word (``"vrf"``, ``"domain"`` …).
        flavor: ``"name"`` or ``"dn"`` — how the Rs points at its target.
        rs_targets: Concrete/abstract target classes of the alias that map to
            **this** Rs class in ``REFERENCE_MAP`` (decides which alias a
            concrete target belongs to).
        expansion: Full target expansion of the alias (curated class plus
            ``TARGET_SUBCLASSES``) — what the push-time resolver will search,
            used to simulate its lookup.
        dn_safe: The alias maps to exactly one Rs class overall, so
            ``bind_dn(alias=...)`` passes its own ambiguity guard.
    """

    alias: str
    flavor: Literal["name", "dn"]
    rs_targets: frozenset[str]
    expansion: frozenset[str]
    dn_safe: bool


@cache
def _bind_inversion() -> dict[str, dict[str, tuple[_BindAlias, ...]]]:
    """``{owner_class: {rs_class: aliases}}`` — the curated binds, inverted.

    Only *direct* edges are kept (the Rs physically parented under the owner
    class): an inverse-side alias (``fvCtx.bind(l3out=...)``) materialises
    under the target, where the target's own direct alias covers the import.
    """
    from niwaki.domain._child_map import REFERENCE_MAP, TARGET_SUBCLASSES

    out: dict[str, dict[str, tuple[_BindAlias, ...]]] = {}
    for owner, table in _tables().binds.items():
        direct = REFERENCE_MAP.get(owner, {})
        for alias, curated in sorted(table.items()):
            expansion = frozenset((curated, *TARGET_SUBCLASSES.get(curated, ())))
            per_rs: dict[tuple[str, Literal["name", "dn"]], set[str]] = {}
            for target in expansion:
                if target in direct:
                    per_rs.setdefault(direct[target], set()).add(target)
            for (rs_cls, flavor), targets in per_rs.items():
                entry = _BindAlias(
                    alias=alias,
                    flavor=flavor,
                    rs_targets=frozenset(targets),
                    expansion=expansion,
                    dn_safe=len(per_rs) == 1,
                )
                bucket = out.setdefault(owner, {})
                bucket[rs_cls] = (*bucket.get(rs_cls, ()), entry)
    return out


def _wire_name_of(cls: type[ManagedObject], field: str) -> str:
    """Wire spelling of a model field (its serialization alias, or itself)."""
    info = cls.model_fields[field]
    return info.serialization_alias if isinstance(info.serialization_alias, str) else field


def _alias_map_of(cls: type[ManagedObject]) -> dict[str, str]:
    """``{wire_name: readable_field}`` covering every field of *cls*."""
    mapping = {f: f for f in cls.model_fields if f != "children"}
    mapping.update(cls._get_alias_map())  # pyright: ignore[reportPrivateUsage]
    return mapping


def _attach_trusted(
    parent: DesignNode, cls: type[ManagedObject], label: str, naming: dict[str, Any]
) -> Cursor:
    """Attach a typed child the snapshot vouches for, skipping the containment gate.

    The trusted twin of ``_cursor._attach``: naming still validates through
    the model and a same-RN sibling still raises, but the containment check
    is waived — the object exists on the fabric under exactly this parent,
    which is stronger evidence than the SDK's derived tables.

    Raises:
        pydantic.ValidationError: A naming value violates the model.
        DuplicateDeclarationError: Same class + RN already declared here.
    """
    child = DesignNode(cls, label, naming, {}, parent, position=None)
    rn = child.rn  # constructs + validates the MO
    if any(s.aci_class == child.aci_class and s.rn == rn for s in parent.children):
        raise DuplicateDeclarationError(f"{child.path()} is already declared.")
    parent.children.append(child)
    return Cursor(child)


def _is_schema_default(cls_name: str, wire: str, value: str) -> bool:
    """Whether *value* is the property's schema default, per the catalogue.

    The gate for dropping a model-refused value as an unset marker: Cisco
    spells "not configured" outside a property's own value space
    (``vmac="not-applicable"``, ``vrfIndex="0"`` under a ``1..N`` range),
    and the annotation — built from the documented range — cannot hold it.
    A property with no declared default answers ``False``.
    """
    from niwaki.query._catalog import catalog

    try:
        default = catalog().prop_meta(cls_name, wire).default
    except KeyError:
        return False
    return default is not None and str(default) == value


# ── The importer ──────────────────────────────────────────────────────────────


@dataclass
class _Importer:
    """One :func:`to_design` run: policies, snapshot indexes, collected problems."""

    on_unknown: Literal["raise", "raw"]
    redacted: Literal["raise", "skip"]
    problems: list[ImportProblem]
    class_of: dict[str, str]  # dn → snapshot class
    primary: dict[str, str]  # dn → primary naming value ("" when unnamed/unknown)
    names: dict[str, dict[str, list[str]]]  # class → primary name → [dns]

    # ── Index (pre-pass) ──────────────────────────────────────────────────────

    def index(self, node: _SnapNode, dn: str) -> None:
        """Record every snapshot object by DN and by (class, primary name).

        The (class, name) index mirrors what the push-time resolver will see
        once the design is built, so closed-world checks can be simulated
        during the walk (forward references included).
        """
        from niwaki.query._catalog import catalog

        cls_name = node["class"]
        self.class_of[dn] = cls_name
        name = ""
        try:
            rn_format = catalog().rn_format(cls_name)
            values = naming_values(node["rn"], rn_format)
            # Primary naming prop = the FIRST placeholder of the RN format
            # (catalog().class_meta().naming is an unordered set).
            first = rn_format.partition("{")[2].partition("}")[0]
            name = values.get(first, "")
        except (KeyError, ValueError):
            pass  # unknown class or unparseable RN — indexed by DN only
        self.primary[dn] = name
        if name:
            self.names.setdefault(cls_name, {}).setdefault(name, []).append(dn)
        for child in node["children"]:
            self.index(child, f"{dn}/{child['rn']}")

    def _scoped_winner(self, owner_dn: str, classes: frozenset[str], name: str) -> str | None:
        """Simulate the resolver's scoped lookup on the snapshot index.

        Mirrors ``_lookup_target``: among every snapshot object matching one
        of *classes* and *name*, the one sharing the longest DN prefix
        (nearest enclosing scope) with *owner_dn* wins; a tie or an empty
        candidate set answers ``None`` — the caller falls back rather than
        emitting a bind the push would refuse or resolve differently.
        """
        candidates = [dn for cls in classes for dn in self.names.get(cls, {}).get(name, ())]
        if not candidates:
            return None
        owner_segments = split_dn(owner_dn)

        def depth(dn: str) -> int:
            shared = 0
            for a, b in zip(owner_segments, split_dn(dn), strict=False):
                if a != b:
                    break
                shared += 1
            return shared

        scored = sorted(((depth(dn), dn) for dn in candidates), reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    # ── Problem / policy helpers ──────────────────────────────────────────────

    def _problem(self, dn: str, kind: _ProblemKind, detail: str) -> None:
        self.problems.append(ImportProblem(dn=dn, kind=kind, detail=detail))

    @staticmethod
    def _catalog_knows(cls_name: str, wire: str) -> bool:
        """Whether the shipped catalogue knows *wire* as a property of *cls_name*."""
        from niwaki.query._catalog import catalog

        try:
            return wire in catalog().class_meta(cls_name).wire_to_readable
        except KeyError:
            return False

    def _screen_attrs(self, dn: str, attrs: dict[str, str]) -> dict[str, str]:
        """Normalise a node's wire attributes — once per node, before any route.

        Two screens, both value-level and route-independent:

        - **Empty strings drop silently**: ``""`` is the wire spelling of
          "not configured", and the design layer cannot emit it by contract
          (``to_apic`` skips it) — dropping is the identity transform.
        - **The redacted-secret sentinel never reaches a design**: collected
          as a problem by default, dropped under ``redacted="skip"``.

        Called exactly once per snapshot node (the screened dict is what
        every import route receives), so a redacted value reports exactly
        one problem however many routes the node falls through.
        """
        from niwaki.snapshot import REDACTED

        kept = {wire: value for wire, value in attrs.items() if value != "" and value != REDACTED}
        if self.redacted == "raise":
            elided = sorted(wire for wire, value in attrs.items() if value == REDACTED)
            if elided:
                self._problem(
                    dn,
                    "redacted-value",
                    f"propert{'ies' if len(elided) > 1 else 'y'} {elided!r} hold(s) the "
                    "redacted-secret sentinel (secrets never read back from an APIC); "
                    "pass redacted='skip' to drop them, then re-declare the real "
                    "values on the imported design",
                )
        return kept

    # ── Walk ──────────────────────────────────────────────────────────────────

    def walk_children(self, cursor: Cursor, node: _SnapNode, dn: str) -> None:
        for child in node["children"]:
            self._import_node(cursor, child, f"{dn}/{child['rn']}", dn)

    def _import_node(self, parent: Cursor, node: _SnapNode, dn: str, parent_dn: str) -> None:
        """Import one snapshot object under *parent* — verb, maker, bind, or raw."""
        cls_name = node["class"]
        parent_cls = parent.design_node.aci_class
        parent_node = parent.design_node
        grandparent_cls = parent_node.parent.aci_class if parent_node.parent else None
        childless = not node["children"]
        attrs = self._screen_attrs(dn, node["attributes"])

        if childless:
            verb_spec = _verb_inversion().get((parent_cls, cls_name))
            if verb_spec is not None and self._try_verb(
                parent, node, attrs, dn, parent_dn, verb_spec
            ):
                return
        label = _maker_inversion().get((parent_cls, cls_name))
        if (
            label is not None
            and _maker_allowed(parent_cls, label, grandparent_cls)
            and self._try_maker(parent, node, attrs, dn, label)
        ):
            return
        if childless:
            aliases = _bind_inversion().get(parent_cls, {}).get(cls_name)
            if aliases and self._try_bind(parent, node, attrs, dn, parent_dn, aliases):
                return
        self._raw_node(parent, node, attrs, dn)

    # ── Relationship preparation (shared by verbs and binds) ─────────────────

    def _prepare_rs(
        self, rs_cls_name: str, node: _SnapNode, attrs: dict[str, str]
    ) -> tuple[Literal["name", "dn"], str, dict[str, Any]] | None:
        """Translate a relationship node into ``(flavor, target, ref attrs)``.

        *attrs* is the node's already-screened attribute dict (empty strings
        and redacted values gone — the screen ran, and reported, once at
        :meth:`_import_node`).  A model-refused value that is the property's
        schema default drops here too (an unset marker, same rule as
        :meth:`_apply_attrs`).

        Returns ``None`` whenever the node cannot be *provably* re-expressed
        as a verb/bind — an unknown property, an empty target, a genuinely
        refused value.  The caller then falls back to the raw ladder, which
        owns the corresponding policies (escape or problem); nothing is
        reported here to avoid double-counting.
        """
        try:
            cls = _load_class(rs_cls_name)
        except KeyError:
            return None
        alias_map = _alias_map_of(cls)
        readable: dict[str, Any] = {}
        for wire, value in attrs.items():
            field = alias_map.get(wire)
            if field is None:
                return None  # unknown property — the raw ladder owns the policy
            readable[field] = value
        try:
            readable.update(naming_values(node["rn"], cls._rn_format))  # pyright: ignore[reportPrivateUsage]
        except ValueError:
            return None
        flavor: Literal["name", "dn"] = "dn" if "target_dn" in cls.model_fields else "name"
        target = str(readable.pop("target_dn" if flavor == "dn" else "name", ""))
        if not target:
            return None  # unformed relation — carried verbatim by the raw ladder
        target_field = "target_dn" if flavor == "dn" else "name"
        while True:
            try:
                # Exactly what the push-time resolver will build; validating
                # now keeps escapes on the raw ladder (a bind has no raw_set).
                cls.model_validate({target_field: target, **readable})
                return flavor, target, readable
            except pydantic.ValidationError as exc:
                defaults = [
                    field
                    for field in {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
                    if field in readable
                    and _is_schema_default(
                        rs_cls_name, _wire_name_of(cls, field), str(readable[field])
                    )
                ]
                if not defaults:
                    return None  # genuinely refused — the raw ladder escapes it
                for field in defaults:
                    del readable[field]

    def _target_arg(self, target: str, attrs: dict[str, Any]) -> str | Ref:
        return Ref(target=target, attrs=attrs) if attrs else target

    def _try_verb(
        self,
        parent: Cursor,
        node: _SnapNode,
        attrs: dict[str, str],
        dn: str,
        parent_dn: str,
        spec: tuple[str, str],
    ) -> bool:
        """Re-express a relationship as its curated verb, when provably equivalent."""
        from niwaki.domain._child_map import TARGET_SUBCLASSES

        verb, target_cls = spec
        prepared = self._prepare_rs(node["class"], node, attrs)
        if prepared is None:
            return False
        flavor, target, rest = prepared
        expansion = frozenset((target_cls, *TARGET_SUBCLASSES.get(target_cls, ())))
        if flavor == "name":
            if self._scoped_winner(parent_dn, expansion, target) is None:
                return False  # dangling or ambiguous — the raw ladder is exact
            arg = target
        else:
            target_name = self.primary.get(target, "")
            if not target_name:
                return False  # target outside the snapshot — verbs have no DN form
            if self._scoped_winner(parent_dn, expansion, target_name) != target:
                return False
            arg = target_name
        parent._verb(verb, self._target_arg(arg, rest))  # pyright: ignore[reportPrivateUsage]
        return True

    def _try_bind(
        self,
        parent: Cursor,
        node: _SnapNode,
        attrs: dict[str, str],
        dn: str,
        parent_dn: str,
        aliases: tuple[_BindAlias, ...],
    ) -> bool:
        """Re-express a relationship as ``bind()``/``bind_dn()``, when provably equivalent."""
        prepared = self._prepare_rs(node["class"], node, attrs)
        if prepared is None:
            return False
        flavor, target, rest = prepared
        candidates = [a for a in aliases if a.flavor == flavor]
        if flavor == "name":
            # The alias whose expansion resolves this name in the snapshot —
            # unique, or the inversion would be a guess.
            hits = [a for a in candidates if self._scoped_winner(parent_dn, a.expansion, target)]
            if len(hits) != 1:
                return False
            parent.bind(**{hits[0].alias: self._target_arg(target, rest)})
            return True

        # DN flavor: prefer the closed-world bind when the target is in the
        # snapshot and the resolver would provably land on it; otherwise the
        # raw-DN escape (bind_dn) is exact by construction.
        target_cls = self.class_of.get(target)
        if target_cls is not None:
            hits = [a for a in candidates if target_cls in a.rs_targets]
            target_name = self.primary.get(target, "")
            if (
                len(hits) == 1
                and target_name
                and self._scoped_winner(parent_dn, hits[0].expansion, target_name) == target
            ):
                parent.bind(**{hits[0].alias: self._target_arg(target_name, rest)})
                return True
        dn_safe = sorted(a.alias for a in candidates if a.dn_safe)
        if not dn_safe:
            return False
        # Several dn-safe aliases resolve to the same Rs class (measured: the
        # two curated collisions are both DN-flavored) — the emitted wire
        # object is identical whichever is named, so the first is canonical.
        try:
            parent.bind_dn(**{dn_safe[0]: self._target_arg(target, rest)})
        except DesignError:
            return False
        return True

    # ── Makers ────────────────────────────────────────────────────────────────

    def _try_maker(
        self, parent: Cursor, node: _SnapNode, attrs: dict[str, str], dn: str, label: str
    ) -> bool:
        """Import through the curated maker; naming from the RN, attrs via set()."""
        cls_name = node["class"]
        try:
            cls = _load_class(cls_name)
        except KeyError:
            return False
        try:
            naming = naming_values(node["rn"], cls._rn_format)  # pyright: ignore[reportPrivateUsage]
        except ValueError as exc:
            self._problem(dn, "structure", f"RN does not match the {cls_name} format: {exc}")
            return True  # handled (with its problem) — the raw ladder cannot parse it either
        try:
            child = _make_child(parent.design_node, label, cls_name, (), dict(naming))
        except pydantic.ValidationError as exc:
            self._problem(dn, "invalid-value", f"{cls_name} naming refused: {exc}")
            return True
        except DesignError as exc:
            self._problem(dn, "structure", str(exc))
            return True
        self._apply_attrs(child, cls, attrs, dn)
        self.walk_children(child, node, dn)
        return True

    # ── Attributes (typed nodes) ──────────────────────────────────────────────

    def _apply_attrs(
        self, cursor: Cursor, cls: type[ManagedObject], attrs: dict[str, str], dn: str
    ) -> None:
        """Apply a node's screened wire attributes to a typed node — never silently.

        Wire names translate to readable fields and go through ``set()`` (full
        model validation).  A field the model refuses is resolved by the value
        policy: the property's schema default drops (an unset marker Cisco
        spells outside the annotation — ``not-applicable``, a ``0`` below the
        documented range), anything else escapes to ``raw_set()`` — the APIC
        served it, the wire is authoritative, and this subsumes the unknown-
        enum-member rule.  Unknown wire properties follow the ``on_unknown``
        policy.
        """
        alias_map = _alias_map_of(cls)
        naming_wire = {
            _wire_name_of(cls, prop)
            for prop in cls._naming_props  # pyright: ignore[reportPrivateUsage]
        }
        readable: dict[str, Any] = {}
        escape_known: dict[str, str] = {}
        escape_unknown: dict[str, str] = {}
        for wire, value in attrs.items():
            if wire in naming_wire:
                continue  # identity — fixed by the maker/RN, not re-set
            field = alias_map.get(wire)
            if field is None:
                if self._catalog_knows(cls._aci_class, wire):  # pyright: ignore[reportPrivateUsage]
                    # The catalogue vouches for the property even though the
                    # generated model lacks it (an extraction gap, measured
                    # live: commHttps.dhParam) — the wire channel carries it,
                    # no opt-in needed.
                    escape_known[wire] = value
                elif self.on_unknown == "raw":
                    escape_unknown[wire] = value
                else:
                    self._problem(
                        dn,
                        "unknown-property",
                        f"{cls.__name__} has no property {wire!r} in this SDK's "
                        "schema baseline; pass on_unknown='raw' to carry it verbatim",
                    )
                continue
            readable[field] = value

        cls_name = cls._aci_class or cls.__name__  # pyright: ignore[reportPrivateUsage]
        remaining = readable
        while remaining:
            try:
                cursor.set(**remaining)
                break
            except pydantic.ValidationError as exc:
                failing = {
                    str(err["loc"][0])
                    for err in exc.errors()
                    if err["loc"] and str(err["loc"][0]) in remaining
                }
                if not failing:
                    self._problem(dn, "invalid-value", f"{cls.__name__} refused: {exc}")
                    break
                for field in failing:
                    value = remaining.pop(field)
                    wire = _wire_name_of(cls, field)
                    if not _is_schema_default(cls_name, wire, str(value)):
                        # The APIC served it and it is not the unset marker:
                        # the wire is authoritative, the annotation is what
                        # lies — carry it on the wire channel, never drop.
                        escape_known[wire] = str(value)
        if escape_known:
            cursor.raw_set(**escape_known)
        if escape_unknown:
            # Unknown to the model AND (possibly) the catalogue — raw_set's
            # validation would refuse them, which is the very reason the
            # caller opted into on_unknown="raw".  The node channel is the
            # documented bypass; values are already wire strings.
            cursor.design_node.raw_attrs.update(escape_unknown)

    # ── Raw ladder ────────────────────────────────────────────────────────────

    def _raw_node(self, parent: Cursor, node: _SnapNode, attrs: dict[str, str], dn: str) -> None:
        """Fallback: import through the wire-name doors (typed, catalogue, direct)."""
        from niwaki.domain._child_map import CLASS_PKG
        from niwaki.query._catalog import catalog

        cls_name = node["class"]
        if cls_name in CLASS_PKG:
            # Generated model — the typed raw route, with the value escapes
            # the public raw() cannot offer.
            cls = _load_class(cls_name)
            try:
                naming = naming_values(node["rn"], cls._rn_format)  # pyright: ignore[reportPrivateUsage]
            except ValueError as exc:
                self._problem(dn, "structure", f"RN does not match the {cls_name} format: {exc}")
                return
            try:
                child = _attach(parent.design_node, cls, cls_name, dict(naming), {})
            except pydantic.ValidationError as exc:
                self._problem(dn, "invalid-value", f"{cls_name} naming refused: {exc}")
                return
            except DuplicateDeclarationError:
                self._problem(
                    dn,
                    "structure",
                    f"{cls_name} {node['rn']!r} appears twice under the same "
                    "parent — a snapshot document never repeats a DN",
                )
                return
            except DesignError:
                # A containment the SDK's tables lack — but the object EXISTS
                # on the fabric under exactly this parent, and the fabric is
                # the authority on its own edges (measured live: three holes
                # found on one sim).  Trust the snapshot; a corrupted document
                # fails at the only judge left, the controller.
                try:
                    child = _attach_trusted(parent.design_node, cls, cls_name, dict(naming))
                except pydantic.ValidationError as exc:
                    self._problem(dn, "invalid-value", f"{cls_name} naming refused: {exc}")
                    return
                except DuplicateDeclarationError:
                    self._problem(
                        dn,
                        "structure",
                        f"{cls_name} {node['rn']!r} appears twice under the same "
                        "parent — a snapshot document never repeats a DN",
                    )
                    return
            self._apply_attrs(child, cls, attrs, dn)
            self.walk_children(child, node, dn)
            return

        try:
            meta = catalog().class_meta(cls_name)
            rn_format = catalog().rn_format(cls_name)
        except KeyError:
            if self.on_unknown == "raw":
                self._raw_direct(parent, node, dn, {}, dict(attrs))
            else:
                self._problem(
                    dn,
                    "unknown-class",
                    f"{cls_name} is not in this SDK's catalogue (a newer firmware?); "
                    "pass on_unknown='raw' to carry the subtree verbatim",
                )
            return

        try:
            naming = naming_values(node["rn"], rn_format)
        except ValueError as exc:
            self._problem(dn, "structure", f"RN does not match the {cls_name} format: {exc}")
            return
        known: dict[str, str] = {}
        escape_unknown: dict[str, str] = {}
        for wire, value in attrs.items():
            if wire in meta.wire_to_readable:
                known[wire] = value
            elif self.on_unknown == "raw":
                escape_unknown[wire] = value
            else:
                self._problem(
                    dn,
                    "unknown-property",
                    f"{cls_name} has no property {wire!r} in this SDK's catalogue; "
                    "pass on_unknown='raw' to carry it verbatim",
                )
        try:
            child = parent.raw(cls_name, **{**known, **naming})
        except DuplicateDeclarationError:
            self._problem(
                dn,
                "structure",
                f"{cls_name} {node['rn']!r} appears twice under the same "
                "parent — a snapshot document never repeats a DN",
            )
            return
        except DesignError:
            # A containment the SDK's tables lack, on a class the catalogue
            # knows (measured live: commTelnet under commPol, uiSettingsCont
            # under polUni, aaaAppUser under aaaUserEp) — the fabric is the
            # authority on its own edges: trust the snapshot.
            self._raw_direct(
                parent,
                node,
                dn,
                naming,
                {k: v for k, v in known.items() if k not in naming} | escape_unknown,
            )
            return
        if escape_unknown:
            child.design_node.raw_attrs.update(escape_unknown)
        self.walk_children(child, node, dn)

    def _raw_direct(
        self,
        parent: Cursor,
        node: _SnapNode,
        dn: str,
        naming: dict[str, str],
        rest: dict[str, str],
    ) -> None:
        """Construct a raw node the snapshot vouches for, bypassing catalogue checks.

        Only reachable under ``on_unknown="raw"``: the class (or its
        containment) is outside the shipped catalogue, so the snapshot's own
        identity — its RN, verbatim — is the only authority left.  Naming
        values stay out of *rest* so they are not emitted twice.
        """
        if any(
            sibling.aci_class == node["class"] and sibling.rn == node["rn"]
            for sibling in parent.design_node.children
        ):
            self._problem(
                dn,
                "structure",
                f"{node['class']} {node['rn']!r} appears twice under the same "
                "parent — a snapshot document never repeats a DN",
            )
            return
        rest = {wire: value for wire, value in rest.items() if wire not in naming}
        raw = RawDesignNode(node["class"], dict(naming), node["rn"], rest, parent.design_node)
        parent.design_node.children.append(raw)
        self.walk_children(Cursor(raw), node, dn)


def _resolve_child_class(parent_cls: str, segment: str) -> str:
    """Resolve one RN segment to its class among *parent_cls*'s children.

    The 117 RN prefixes shared across the whole schema are ambiguous
    *globally*, never among one parent's children — measured: zero RN-format
    skeleton collisions inside any of the 3 049 containment tables.  The
    walk from ``polUni`` down a scope DN therefore resolves each segment
    uniquely.

    Raises:
        DesignError: No child class of *parent_cls* matches *segment*, or —
            should curation ever break the measured uniqueness — more than
            one does.
    """
    from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG
    from niwaki.query._catalog import catalog

    # The navigation table keys the top level as "_root"; the generated
    # model's _contains completes the picture for typed parents.
    table_key = "_root" if parent_cls == "polUni" else parent_cls
    candidates = set(CHILD_MAP.get(table_key, {}).values())
    if parent_cls in CLASS_PKG:
        candidates |= set(_load_class(parent_cls)._contains)  # pyright: ignore[reportPrivateUsage]

    matches: list[str] = []
    for cls_name in candidates:
        try:
            naming_values(segment, catalog().rn_format(cls_name))
        except (KeyError, ValueError):
            continue
        matches.append(cls_name)
    if not matches:
        # Third authority: the catalogue's own DN grammar — the same authority
        # _may_contain gained in it.4 (the scope walk shipped in it.3, before
        # it, and never got the seam re-audited).  A containment edge both
        # generated tables dropped is still provable from the child's DN
        # formats: the parent's RN format is the second-to-last segment.
        matches = _grammar_children(parent_cls, segment)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise DesignError(
            f"scope walk: no child class of {parent_cls} matches the RN segment {segment!r}."
        )
    raise DesignError(
        f"scope walk: RN segment {segment!r} is ambiguous under {parent_cls} "
        f"({', '.join(sorted(matches))})."
    )


@cache
def _rn_format_rows() -> tuple[tuple[str, str], ...]:
    """Every catalogue ``(class_name, rn_format)`` pair, scanned once."""
    from niwaki.query._catalog import catalog

    return tuple(catalog()._rn_format_rows())  # pyright: ignore[reportPrivateUsage]


def _grammar_children(parent_cls: str, segment: str) -> list[str]:
    """Classes the DN grammar proves under *parent_cls* that match *segment*.

    The slow path of :func:`_resolve_child_class`, walked only when neither
    CHILD_MAP nor the generated ``_contains`` knows the edge (measured live:
    ``uiSettingsCont`` under ``polUni``, ``aaaAppUser`` under ``aaaUserEp``,
    ``commTelnet`` under ``commPol``).  Shape-matches the segment against
    every catalogued RN format, then keeps only the classes whose
    ``dn_formats`` place the parent's RN format immediately above — the same
    proof :meth:`Cursor._may_contain` accepts.
    """
    from niwaki._dn import split_dn
    from niwaki.query._catalog import catalog

    parent_rn = "uni" if parent_cls == "polUni" else catalog().rn_format(parent_cls)
    if not parent_rn:
        return []
    matches: list[str] = []
    for cls_name, rn_fmt in _rn_format_rows():
        try:
            naming_values(segment, rn_fmt)
        except ValueError:
            continue
        for fmt in catalog().dn_formats(cls_name):
            segments = split_dn(fmt)
            if len(segments) >= 2 and segments[-2] == parent_rn:
                matches.append(cls_name)
                break
    return matches


def _bare_ancestor(parent: Cursor, parent_dn: str, segment: str) -> tuple[Cursor, str]:
    """Declare one attribute-less ancestor of a scoped import (Day-2 upsert).

    The ancestors of a scoped snapshot are not part of the capture — they
    exist on the fabric by definition — so they enter the design bare: the
    push upserts them without touching a single attribute (the established
    Day-2 pattern).
    """
    from niwaki.domain._child_map import CLASS_PKG
    from niwaki.query._catalog import catalog

    parent_cls = parent.design_node.aci_class
    cls_name = _resolve_child_class(parent_cls, segment)
    dn = f"{parent_dn}/{segment}"
    if cls_name in CLASS_PKG:
        cls = _load_class(cls_name)
        naming = naming_values(segment, cls._rn_format)  # pyright: ignore[reportPrivateUsage]
        label = _maker_inversion().get((parent_cls, cls_name))
        if label is not None and _maker_allowed(
            parent_cls,
            label,
            parent.design_node.parent.aci_class if parent.design_node.parent else None,
        ):
            return _make_child(parent.design_node, label, cls_name, (), dict(naming)), dn
        return _attach(parent.design_node, cls, cls_name, dict(naming), {}), dn
    wire_naming = naming_values(segment, catalog().rn_format(cls_name))
    return parent.raw(cls_name, **wire_naming), dn


def _payload_tree(envelope: dict[str, Any], *, root: bool = True) -> _SnapNode:
    """One APIC payload envelope → the snapshot node shape (fail-loud).

    The RN is recomputed from the catalogue's RN format and the envelope's
    own naming values — a payload that omits a naming value cannot name the
    object it claims to declare, and is refused rather than guessed at.
    """
    from niwaki.models._wire import to_wire
    from niwaki.query._catalog import catalog

    if not isinstance(envelope, dict) or len(envelope) != 1:  # pyright: ignore[reportUnnecessaryIsInstance]
        found = sorted(envelope) if isinstance(envelope, dict) else type(envelope).__name__  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DesignError(
            f"from_payload(): an envelope holds exactly one class key, found {found!r}."
        )
    (cls_name,) = envelope.keys()
    inner = envelope[cls_name]
    if not isinstance(inner, dict) or not isinstance(inner.get("attributes", {}), dict):
        raise DesignError(f"from_payload(): {cls_name} is not a well-formed envelope body.")
    if not isinstance(inner.get("children", []), list):
        raise DesignError(f"from_payload(): {cls_name} carries a non-list 'children'.")
    attrs = {str(k): v for k, v in dict(inner.get("attributes", {})).items()}
    if "status" in attrs:
        raise DesignError(
            f"from_payload(): {cls_name} carries a 'status' directive — a "
            "payload with write directives describes an operation, not a "
            "state, and cannot import as a design."
        )
    attrs.pop("dn", None)  # identity — recomputed from the tree shape
    attrs.pop("rn", None)
    wire_attrs = {k: v if isinstance(v, str) else to_wire(v) for k, v in attrs.items()}
    if cls_name == "polUni":
        if not root:
            raise DesignError(
                "from_payload(): polUni nested below the root — a payload "
                "declares the config universe exactly once."
            )
        rn = "uni"
    else:
        try:
            rn_format = catalog().rn_format(cls_name)
            naming = catalog().class_meta(cls_name).naming
        except KeyError:
            raise DesignError(
                f"from_payload(): unknown ACI class {cls_name!r} — not in the catalogue."
            ) from None
        missing = sorted(p for p in naming if p not in wire_attrs)
        if missing:
            raise DesignError(
                f"from_payload(): {cls_name} omits its naming propert"
                f"{'ies' if len(missing) > 1 else 'y'} {missing!r} "
                f"(RN format {rn_format!r})."
            )
        # Token-wise fill: a naming VALUE containing a literal "{prop}" must
        # never be re-substituted (str.replace chaining would corrupt identity).
        from niwaki._dn import _tokenize_rn_format

        rn = "".join(
            text if kind == "lit" else wire_attrs[text]
            for kind, text in _tokenize_rn_format(rn_format)
        )
    return {
        "class": cls_name,
        "rn": rn,
        "attributes": wire_attrs,
        "children": [_payload_tree(child, root=False) for child in inner.get("children", [])],
    }


def from_payload(
    payload: dict[str, Any],
    *,
    on_unknown: Literal["raise", "raw"] = "raise",
    redacted: Literal["raise", "skip"] = "raise",
) -> Cursor:
    """Rebuild a design from a raw APIC payload — the inverse of ``to_payload``.

    Accepts the atomic ``polUni`` envelope shape (``{"polUni": {"attributes":
    …, "children": […]}}``) — what ``to_payload()`` emits, what a strict push
    POSTs, and what configuration JSON exported from other tooling commonly
    looks like.  The payload is converted to the snapshot tree shape (RNs
    recomputed from the catalogue, fail-loud on a missing naming value or a
    ``status`` write directive) and handed to the same importer as
    :func:`to_design` — one inversion engine, two doors.

    Args:
        payload: The ``polUni`` envelope dict.
        on_unknown: See :func:`to_design`.
        redacted: See :func:`to_design`.

    Returns:
        The root :class:`~niwaki.design.Cursor` of the rebuilt design.

    Raises:
        DesignError: The envelope is malformed — not rooted on ``polUni``,
            several class keys in one envelope, an unknown class, a missing
            naming value, or a ``status`` directive.
        SnapshotImportError: See :func:`to_design`.

    Example::

        from niwaki.design import from_payload, tenant

        original = tenant("prod")
        original.bd("web")
        clone = from_payload(original.to_payload())
        assert clone.to_payload() == original.to_payload()
    """
    from niwaki.snapshot import Snapshot

    if len(payload) != 1 or next(iter(payload)) != "polUni":
        raise DesignError(
            f"from_payload(): the payload must be one polUni envelope, found {sorted(payload)!r}."
        )
    tree = _payload_tree(payload)
    return to_design(Snapshot(scope="uni", tree=tree), on_unknown=on_unknown, redacted=redacted)


def to_design(
    snapshot: Snapshot,
    *,
    on_unknown: Literal["raise", "raw"] = "raise",
    redacted: Literal["raise", "skip"] = "raise",
) -> Cursor:
    """Rebuild a design from a snapshot — the inverse of ``push``.

    Walks the snapshot tree and re-expresses every captured object in the
    design DSL, preferring the curated vocabulary (makers, ``bind()``, the
    contract verbs) and falling back to the wire-name escape hatches
    (:meth:`~niwaki.design.Cursor.raw` / ``raw_set``) whenever a curated
    inversion cannot be made provably equivalent.  The returned design
    compiles to the same wire payload the snapshot describes: pushing it in
    ``mode="plan"`` against the fabric the snapshot was taken from reports no
    changes.

    A live snapshot carries every configurable property at its current wire
    value, unset markers included; the importer normalises them so the
    strict typed models never see what the fabric spells as "not
    configured": empty strings drop (the design layer cannot emit ``""``
    by contract), a model-refused value equal to the property's schema
    default drops (``vmac="not-applicable"``, ``vrfIndex="0"``), and any
    other model-refused value rides the wire channel verbatim
    (``raw_set`` — the APIC served it, the wire is authoritative).

    Objects whose **DN itself** carries a secret (listed in
    :attr:`~niwaki.snapshot.Snapshot.warnings`) import unchanged — their
    identity cannot be redacted; review the snapshot's warnings before
    sharing anything derived from it.

    Args:
        snapshot: A :class:`~niwaki.snapshot.Snapshot` — the whole
            configuration (scope ``"uni"``) or any narrower config scope
            under it (``"uni/tn-prod"``, ``"uni/infra"``, …).  For a scoped
            snapshot the ancestors of the captured object are rebuilt from
            the scope DN as attribute-less Day-2 upserts: pushing the design
            never touches an attribute the capture does not carry.
        on_unknown: Policy for classes/properties outside this SDK's schema
            baseline (a snapshot from a newer firmware).  ``"raise"``
            (default) collects every occurrence into one
            :class:`~niwaki.exceptions.SnapshotImportError`; ``"raw"``
            carries them verbatim on the wire-attribute channel instead.
        redacted: Policy for values holding the
            :data:`~niwaki.snapshot.REDACTED` sentinel (curated secrets are
            elided at capture).  ``"raise"`` (default) collects them — a
            design pushing the sentinel literally would be wrong;
            ``"skip"`` drops those values from the design.  A relation
            whose *target* value was redacted imports as an unformed
            relation under ``"skip"`` — re-declare it with the real target
            before pushing anywhere.

    Returns:
        The root :class:`~niwaki.design.Cursor` of the rebuilt design
        (a ``polUni`` node), ready for ``to_payload()`` / ``push()``.

    Raises:
        DesignError: The snapshot's scope is outside ``uni``, its tree is
            empty, a ``"uni"`` snapshot is not rooted on ``polUni``, or an
            ancestor segment of a scoped snapshot cannot be resolved.
        SnapshotImportError: One or more items could not be imported —
            collected over the whole tree, never first-fail.  The partial
            design is discarded.

    Example::

        from niwaki import snapshot
        from niwaki.design import to_design

        snap = snapshot.Snapshot.from_json(Path("fabric.json").read_text())
        config = to_design(snap)
        result = config.push(aci, mode="plan")
        assert not result.has_changes   # the design IS the fabric
    """
    from niwaki.design._generated_cursors import design

    tree = snapshot.tree
    if tree is None:
        raise DesignError("to_design(): the snapshot is empty (tree is None).")
    segments = split_dn(snapshot.scope)
    if not segments or segments[0] != "uni":
        raise DesignError(
            f"to_design() imports configuration under 'uni'; this snapshot "
            f"covers {snapshot.scope!r}, which is outside it."
        )

    importer = _Importer(
        on_unknown=on_unknown,
        redacted=redacted,
        problems=[],
        class_of={},
        primary={},
        names={},
    )
    importer.index(tree, snapshot.scope)
    root = design()

    if len(segments) == 1:
        if tree["class"] != "polUni":
            raise DesignError(
                f"to_design(): a 'uni' snapshot must be rooted on polUni, found {tree['class']!r}."
            )
        from niwaki.models._generated.pol.polUni import polUni

        root_attrs = importer._screen_attrs("uni", tree["attributes"])  # pyright: ignore[reportPrivateUsage]
        importer._apply_attrs(root, polUni, root_attrs, "uni")  # pyright: ignore[reportPrivateUsage]
        importer.walk_children(root, tree, "uni")
    else:
        # Scoped snapshot: the capture's root object hangs off a chain of
        # attribute-less ancestors (Day-2 upserts) rebuilt from the scope DN.
        if tree["rn"] != segments[-1]:
            raise DesignError(
                f"to_design(): the snapshot's scope {snapshot.scope!r} ends in "
                f"{segments[-1]!r} but its tree is rooted on {tree['rn']!r} — "
                "the document is inconsistent."
            )
        parent: Cursor = root
        parent_dn = "uni"
        for segment in segments[1:-1]:
            parent, parent_dn = _bare_ancestor(parent, parent_dn, segment)
        importer._import_node(parent, tree, snapshot.scope, parent_dn)  # pyright: ignore[reportPrivateUsage]

    if importer.problems:
        raise SnapshotImportError(sorted(importer.problems, key=lambda p: (p.dn, p.detail)))
    return root
