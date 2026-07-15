"""Design cursors — the fluent build surface of the design DSL.

A :class:`Cursor` wraps one :class:`~niwaki.design._node.DesignNode` and
exposes:

- **makers** (``.app()``, ``.bd()``, ``.epg()``, ``.subnet()`` …) resolved
  from ``domain/vocabulary.yaml``, each creating exactly one APIC child
  object and returning the child's cursor;
- **implicit pop**: a maker that belongs to an ancestor level walks up the
  path and creates there (``.bd(...)`` from an EPG cursor creates under the
  tenant);
- ``set()`` — scalar attributes of the current object, merged and eagerly
  validated through the Pydantic model;
- ``bind()`` / ``provide()`` / ``consume()`` — lazy references resolved at
  push time (forward references allowed);
- ``mo()`` — escape hatch for classes outside the curated vocabulary;
- ``push()`` / ``to_payload()`` — compilation entry points (no I/O during
  construction; transport is injected at push time only).

Cursors are plain value wrappers: capture them in variables, use them in
loops — the mega-chained expression is never mandatory.
"""

from __future__ import annotations

import difflib
from functools import cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, overload

from niwaki.design._node import DesignNode, PendingBind, Ref
from niwaki.design._sugar import apply_sugar
from niwaki.exceptions._design import (
    DesignError,
    DuplicateDeclarationError,
    UnknownMakerError,
)
from niwaki.models.base import ManagedObject

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Coroutine as _Coroutine

    from niwaki.design._push import PlanResult, PushReport
    from niwaki.facade import AsyncNiwaki, Niwaki

PushMode = Literal["strict", "staged", "plan"]

_VOCABULARY_YAML = Path(__file__).parent.parent / "domain" / "vocabulary.yaml"


class _DesignVocabulary(NamedTuple):
    """Curated design vocabulary loaded from ``domain/vocabulary.yaml``.

    Attributes:
        makers: Parent ACI class → {maker name → child ACI class}; the
            ``"polUni"`` table defines the design roots.
        binds:  Cursor ACI class → {bind alias → target ACI class}.
        verbs:  Cursor ACI class → {verb → {"rs": Rs class, "target": class}}.
        sugar:  ACI class → {parameter name → type annotation} — extra typed
            parameters surfaced on generated signatures (runtime:
            :mod:`niwaki.design._sugar`).
        atomic: Classes whose subtree must ship in a single POST in staged
            mode (e.g. ``fabricExplicitGEp``).
        carrier: Non-creatable path-only classes the APIC won't POST or read on
            their own (a VMM provider, ``vmmProvP``) — the push emits no op for
            them and the plan diffs their children instead.
    """

    makers: dict[str, dict[str, str]]
    binds: dict[str, dict[str, str]]
    verbs: dict[str, dict[str, dict[str, str]]]
    sugar: dict[str, dict[str, str]]
    atomic: frozenset[str]
    carrier: frozenset[str]


@cache
def _tables() -> _DesignVocabulary:
    """Load and cache the curated vocabulary from ``domain/vocabulary.yaml``.

    The ``yaml`` import is deferred to keep ``import niwaki.design`` within
    the cold-start budget — the table is only needed on the first maker call.
    """
    import yaml

    with _VOCABULARY_YAML.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return _DesignVocabulary(
        makers=data.get("makers", {}),
        binds=data.get("binds", {}),
        verbs=data.get("verbs", {}),
        sugar=data.get("sugar", {}),
        atomic=frozenset(data.get("atomic", [])),
        carrier=frozenset(data.get("carrier", [])),
    )


@cache
def _load_class(aci_class: str) -> type[ManagedObject]:
    """Import a generated class lazily via ``CLASS_PKG`` (facade pattern)."""
    from niwaki.domain._child_map import CLASS_PKG

    pkg = CLASS_PKG[aci_class]
    mod = import_module(f"niwaki.models._generated.{pkg}.{aci_class}")
    cls: type[ManagedObject] = getattr(mod, aci_class)
    return cls


@cache
def _cursor_class_for(position: str | None) -> type[Cursor]:
    """Return the generated typed cursor class for a curated *position*.

    Positions are dotted maker paths from the ``polUni`` root — identity is
    the path, not the class, so ``infraNodeBlk`` gets a distinct cursor under
    a leaf selector and under a spine selector.  Falls back to the base
    :class:`Cursor` for uncurated nodes (``position is None``) and while
    bootstrapping (before the generated module exists).
    """
    if position is None:
        return Cursor
    try:
        from niwaki.design._generated_cursors import CURSOR_FOR
    except ImportError:
        return Cursor
    return CURSOR_FOR.get(position, Cursor)


def _prune(params: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values — used by generated typed methods to forward only
    the keyword arguments the caller actually provided."""
    return {k: v for k, v in params.items() if v is not None}


def _validate_attr_names(cls: type[ManagedObject], attrs: dict[str, Any]) -> None:
    """Reject attribute names that are not declared Python fields of *cls*.

    ``extra="allow"`` on the models would otherwise silently absorb typos
    into ``model_extra`` — and ``to_apic()`` would silently drop them from
    the payload.  Wire aliases are redirected to their Python name.

    Raises:
        DesignError: An attribute is unknown, or an ACI wire name was used
            instead of the human-readable Python field name.
    """
    known = set(cls.model_fields) - {"children"}
    unknown = [key for key in attrs if key not in known]
    if not unknown:
        return
    alias_map = cls._get_alias_map()  # pyright: ignore[reportPrivateUsage]
    for key in unknown:
        if key in alias_map:
            raise DesignError(
                f"{cls.__name__}: use the Python field name "
                f"{alias_map[key]!r} instead of the ACI wire name {key!r}."
            )
    hints = {key: difflib.get_close_matches(key, sorted(known), n=1) for key in unknown}
    details = ", ".join(
        f"{key!r}" + (f" (did you mean {hint[0]!r}?)" if (hint := hints[key]) else "")
        for key in unknown
    )
    raise DesignError(f"{cls.__name__} has no attribute(s) {details}.")


def _unwrap_ref(target: Any) -> tuple[str, dict[str, Any]]:
    """Split a bind target into ``(name_or_dn, relationship attributes)``.

    A plain string is a pure edge; a :class:`~niwaki.design._node.Ref` also
    carries fields for the relationship object itself.
    """
    if isinstance(target, Ref):
        return str(target.target), dict(target.attrs)
    return str(target), {}


def _split_naming(
    cls: type[ManagedObject],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split maker arguments into naming props and scalar attributes.

    Positional args map onto the class naming props in order; keyword args
    matching a naming prop are naming, everything else is an attribute.

    Raises:
        DesignError: More positional args than naming props, or a naming prop
            supplied both positionally and by keyword.
    """
    props: list[str] = cls._naming_props  # pyright: ignore[reportPrivateUsage]
    if len(args) > len(props):
        raise DesignError(
            f"{cls.__name__} takes at most {len(props)} naming argument(s) "
            f"({', '.join(props) or 'none'}); got {len(args)} positional."
        )
    naming: dict[str, Any] = dict(zip(props, args, strict=False))
    attrs: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in props:
            if key in naming:
                raise DesignError(f"Naming prop {key!r} given both positionally and by keyword.")
            naming[key] = value
        else:
            attrs[key] = value
    return naming, attrs


class Cursor:
    """A position in a design tree, exposing the curated build vocabulary.

    Do not instantiate directly — obtain the root cursor from
    :func:`niwaki.design.tenant` and children from maker calls.
    """

    __slots__ = ("_node",)

    def __init__(self, node: DesignNode) -> None:
        self._node = node

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def design_node(self) -> DesignNode:
        """The underlying design node (read-only structural handle).

        Deliberately verbose: short names on the cursor belong to the curated
        vocabulary (``.node()`` is a maker under a route reflector or a vPC
        pair) — the generator enforces that no maker shadows the base cursor
        API.
        """
        return self._node

    @property
    def dn(self) -> str:
        """Distinguished Name this node will occupy once pushed.

        Returns:
            DN string rooted at ``uni`` (e.g. ``"uni/tn-prod/BD-web"``).
        """
        return self._node.dn()

    def __repr__(self) -> str:
        return f"<Cursor {self._node.path()}>"

    # ── Makers (dynamic dispatch, ancestor walk) ──────────────────────────────

    def __getattr__(self, name: str) -> Callable[..., Cursor]:
        """Resolve *name* as a maker on this level or the nearest ancestor.

        Returns:
            A callable ``maker(*naming, **attrs) -> Cursor`` creating the
            child object and returning its cursor.

        Raises:
            UnknownMakerError: *name* is a maker at no level of the path.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        # Resolving here (not at call time) makes unknown names fail at the
        # attribute access — hasattr() and typos surface immediately.
        owner, child_aci_class = self._resolve_maker_level(name)

        def _maker(*args: Any, **kwargs: Any) -> Cursor:
            return _make_child(owner, name, child_aci_class, args, kwargs)

        return _maker

    def _resolve_maker_level(self, name: str) -> tuple[DesignNode, str]:
        """Find the nearest ancestor-or-self level owning maker *name*.

        Returns:
            ``(owner_node, child_aci_class_class)``.

        Raises:
            UnknownMakerError: *name* is a maker at no level of the path.
        """
        makers = _tables().makers
        for level in self._node.ancestors_and_self():
            table = makers.get(level.aci_class, {})
            if name in table:
                return level, table[name]
        available = sorted(
            {
                maker
                for level in self._node.ancestors_and_self()
                for maker in makers.get(level.aci_class, {})
            }
        )
        hint = difflib.get_close_matches(name, available, n=1)
        suggestion = f" Did you mean {hint[0]!r}?" if hint else ""
        raise UnknownMakerError(
            f"No maker {name!r} at any level of {self._node.path()}. "
            f"Available makers on this path: {', '.join(available)}.{suggestion} "
            "Use .mo(Class, ...) for classes outside the curated vocabulary."
        )

    def _invoke_maker(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Cursor:
        """Runtime maker shared by dynamic dispatch and generated cursors."""
        owner, child_aci_class = self._resolve_maker_level(name)
        return _make_child(owner, name, child_aci_class, args, kwargs)

    # ── Attributes ────────────────────────────────────────────────────────────

    def set(self, **attrs: Any) -> Cursor:
        """Set scalar attributes on the current object.

        Values are merged with previously set attributes (last call wins) and
        the full attribute set is re-validated through the Pydantic model
        immediately — constraint violations raise at the call site, before
        any push.

        Args:
            **attrs: Field values using the human-readable Python names
                (e.g. ``unicast_routing=True``).  Sugar applies where defined
                (e.g. ``scope="vrf"`` on a contract).

        Returns:
            This cursor, for chaining.

        Raises:
            DesignError: Unknown attribute name, ACI wire name used instead
                of the Python field name, or attempt to change a naming prop.
            pydantic.ValidationError: A value violates the model constraints.
        """
        node = self._node
        sugared = apply_sugar(node.aci_class, attrs)
        naming_props: list[str] = node.cls._naming_props  # pyright: ignore[reportPrivateUsage]
        if fixed := [k for k in sugared if k in naming_props]:
            raise DesignError(
                f"{node.path()}: naming prop(s) {', '.join(map(repr, fixed))} "
                "are fixed at creation and cannot be changed with set()."
            )
        _validate_attr_names(node.cls, sugared)
        merged = {**node.attrs, **sugared}
        node.cls(**node.naming, **merged)
        node.attrs = merged
        return self

    # ── References ────────────────────────────────────────────────────────────

    def bind(self, **targets: Any) -> Cursor:
        """Declare lazy Rs relationships by target vocabulary and name.

        Each ``alias=name`` pair records a reference resolved at push time —
        forward references are allowed, and the relationship class, its
        flavor (name vs DN) and the side it lives on are derived from
        ``REFERENCE_MAP``, so ``.vrf("prod").bind(l3out="prod")`` works even
        though the Rs object lives on the L3Out side.

        The alias is looked up on this level first, then on ancestors
        (``.subnet(...).bind(vrf=...)`` binds the enclosing BD).

        Args:
            **targets: One or more ``alias=name`` pairs (e.g. ``vrf="prod"``).

        Returns:
            This cursor, for chaining.

        Raises:
            DesignError: An alias is not bindable at any level of the path.
        """
        binds = _tables().binds
        for alias, target in targets.items():
            owner = self._find_bind_owner(alias, binds)
            name, attrs = _unwrap_ref(target)
            owner.binds.append(
                PendingBind(
                    kind="bind",
                    alias=alias,
                    target_aci_class=binds[owner.aci_class][alias],
                    target_name=name,
                    attrs=attrs,
                )
            )
        return self

    def _find_bind_owner(self, alias: str, binds: dict[str, dict[str, str]]) -> DesignNode:
        """Nearest ancestor-or-self level where *alias* is a curated bind.

        Raises:
            DesignError: The alias is bindable at no level of the path.
        """
        for level in self._node.ancestors_and_self():
            if alias in binds.get(level.aci_class, {}):
                return level
        available = sorted(
            {a for level in self._node.ancestors_and_self() for a in binds.get(level.aci_class, {})}
        )
        raise DesignError(
            f"No bind alias {alias!r} at any level of {self._node.path()}. "
            f"Available aliases on this path: {', '.join(available) or 'none'}."
        )

    def bind_dn(self, **targets: str | Ref) -> Cursor:
        """Reference objects **outside the design** by raw DN (escape hatch).

        Same aliases as :meth:`bind`, but the value is a full DN and no
        closed-world lookup happens — the DN is trusted as-is and the APIC
        is the one to reject a dangling reference.  Only aliases whose Rs
        class targets by DN qualify; name-flavored aliases must go through
        ``bind()`` (the Rs object physically stores a name, not a DN).

        Args:
            **targets: One or more ``alias=dn`` pairs
                (e.g. ``vlan_pool="uni/infra/vlanns-[shared]-static"``), or a
                :func:`~niwaki.design.ref` when the relation itself carries
                configuration (``ref(dn, immediacy="immediate")``).

        Returns:
            This cursor, for chaining.

        Raises:
            DesignError: The alias is unknown, its relation lives on the
                target side, or it targets by name rather than by DN.
        """
        from niwaki.domain._child_map import REFERENCE_MAP, TARGET_SUBCLASSES

        binds = _tables().binds
        for alias, target in targets.items():
            owner = self._find_bind_owner(alias, binds)
            target_dn, attrs = _unwrap_ref(target)
            target_aci_class = binds[owner.aci_class][alias]

            direct = REFERENCE_MAP.get(owner.aci_class, {})
            entries = {
                entry
                for candidate in (
                    target_aci_class,
                    *TARGET_SUBCLASSES.get(target_aci_class, ()),
                )
                if (entry := direct.get(candidate)) is not None
            }
            if not entries:
                side = (
                    " The relation lives on the target side — declare the "
                    "target in the design and use bind() instead."
                    if REFERENCE_MAP.get(target_aci_class, {}).get(owner.aci_class)
                    else ""
                )
                raise DesignError(
                    f"{owner.path()}: no Rs class from {owner.aci_class} to "
                    f"{target_aci_class} — bind_dn({alias}=...) is not available.{side}"
                )
            if len(entries) > 1:
                raise DesignError(
                    f"{owner.path()}: bind_dn({alias}=...) is ambiguous — "
                    f"{target_aci_class} maps to several Rs classes."
                )
            rs_aci_class, flavor = next(iter(entries))
            if flavor != "dn":
                raise DesignError(
                    f"{owner.path()}: {alias!r} targets by name ({rs_aci_class} "
                    f"stores a tn* name, not a DN) — use bind({alias}=<name>)."
                )
            owner.binds.append(
                PendingBind(
                    kind="bind_dn",
                    alias=alias,
                    target_aci_class=target_aci_class,
                    target_name=target_dn,
                    rs_aci_class=rs_aci_class,
                    flavor="dn",
                    attrs=attrs,
                )
            )
        return self

    def provide(self, contract: str | Ref) -> Cursor:
        """Declare that this EPG provides *contract* (creates ``fvRsProv``).

        Args:
            contract: Name of a contract declared in this design, or a
                :func:`~niwaki.design.ref` when the relation itself carries
                configuration (``ref("web", prio="level1")``).

        Returns:
            This cursor, for chaining.
        """
        return self._verb("provide", contract)

    def consume(self, contract: str | Ref) -> Cursor:
        """Declare that this EPG consumes *contract* (creates ``fvRsCons``).

        Args:
            contract: Name of a contract declared in this design, or a
                :func:`~niwaki.design.ref` when the relation itself carries
                configuration (``ref("web", prio="level1")``).

        Returns:
            This cursor, for chaining.
        """
        return self._verb("consume", contract)

    def intra_epg(self, contract: str | Ref) -> Cursor:
        """Declare *contract* between the endpoints of this EPG (``fvRsIntraEpg``).

        The APIC applies it to traffic that stays inside the group — the EPG
        must be intra-EPG isolated for it to bite.

        Args:
            contract: Name of a contract declared in this design, or a
                :func:`~niwaki.design.ref` when the relation itself carries
                configuration (``ref("web", prio="level1")``).

        Returns:
            This cursor, for chaining.
        """
        return self._verb("intra_epg", contract)

    def _verb(self, verb: str, target_name: str | Ref) -> Cursor:
        """Record a contract verb on the nearest level that supports it.

        Args:
            verb: A verb curated in ``vocabulary.yaml`` (``provide``,
                ``consume``, ``intra_epg``).
            target_name: Name of the contract the verb points at.

        Returns:
            This cursor, for chaining.

        Raises:
            DesignError: No level of the current path declares that verb.
        """
        verbs = _tables().verbs
        name, attrs = _unwrap_ref(target_name)
        for level in self._node.ancestors_and_self():
            spec = verbs.get(level.aci_class, {}).get(verb)
            if spec is not None:
                level.binds.append(
                    PendingBind(
                        kind="verb",
                        alias=verb,
                        target_aci_class=spec["target"],
                        target_name=name,
                        rs_aci_class=spec["rs"],
                        attrs=attrs,
                    )
                )
                return self
        raise DesignError(
            f"{verb}() is not available at any level of {self._node.path()} "
            "(contract verbs apply to EPGs)."
        )

    # ── Escape hatch ──────────────────────────────────────────────────────────

    def mo(self, cls: type[ManagedObject], **kwargs: Any) -> Cursor:
        """Declare a child of an arbitrary generated class (escape hatch).

        For classes outside the curated vocabulary.  Naming props are picked
        from ``kwargs`` by name; the remainder are scalar attributes.

        Args:
            cls: Generated :class:`~niwaki.models.base.ManagedObject` subclass.
            **kwargs: Naming props and attributes.

        Returns:
            Cursor on the new child node.

        Raises:
            DesignError: *cls* is not a valid APIC child of this object.
        """
        naming, attrs = _split_naming(cls, (), kwargs)
        return _attach(self._node, cls, cls.__name__, naming, attrs)

    # ── Compilation / push ────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        """Validate, resolve references, and return the atomic push payload.

        Follows the ``Query.build()`` house pattern: full inspection without
        execution.  The returned dict is exactly what ``push(mode="strict")``
        POSTs to ``/api/mo/uni.json``.

        Returns:
            ``polUni`` envelope dict wrapping the whole design.

        Raises:
            UnresolvedReferenceError: A reference target is not in the design.
            AmbiguousBindError: A bind edge has no Rs class in either
                direction.
        """
        from niwaki.design._push import build_payload

        return build_payload(self._node.root())

    @overload
    def push(
        self, client: Niwaki, *, mode: Literal["strict", "staged"] = "strict"
    ) -> PushReport: ...

    @overload
    def push(self, client: Niwaki, *, mode: Literal["plan"]) -> PlanResult: ...

    @overload
    def push(
        self, client: AsyncNiwaki, *, mode: Literal["strict", "staged"] = "strict"
    ) -> _Coroutine[Any, Any, PushReport]: ...

    @overload
    def push(
        self, client: AsyncNiwaki, *, mode: Literal["plan"]
    ) -> _Coroutine[Any, Any, PlanResult]: ...

    def push(
        self,
        client: Niwaki | AsyncNiwaki,
        *,
        mode: PushMode = "strict",
    ) -> PushReport | PlanResult | _Coroutine[Any, Any, PushReport | PlanResult]:
        """Validate the design and push it through *client*.

        Always operates on the **whole design tree** regardless of which
        cursor it is called on.  Construction never touches the network —
        transport is injected here and only here.

        Modes:
            - ``"strict"`` (default): closed-world validation (every
              reference must resolve inside the design), then one atomic
              nested POST to ``/api/mo/uni.json`` — all or nothing.
            - ``"staged"``: compile to per-object operations executed in
              DN-depth waves (parents before children) with a detailed
              report; a failing wave stops the remaining ones.
            - ``"plan"``: dry run — read the current APIC state and report
              what would be created or changed, pushing nothing.

        Args:
            client: A connected :class:`~niwaki.Niwaki` (returns the
                result directly) or :class:`~niwaki.AsyncNiwaki`
                (returns an awaitable).
            mode: ``"strict"`` | ``"staged"`` | ``"plan"``.

        Returns:
            :class:`~niwaki.design.PushReport` for write modes,
            :class:`~niwaki.design.PlanResult` for ``"plan"`` (wrapped in a
            coroutine for async clients).

        Raises:
            UnresolvedReferenceError: Closed-world validation failed.
            AmbiguousBindError: A bind edge has no Rs class.
            APIError: The APIC rejected a write (strict mode).
            StagedPushError: One or more staged operations failed — carries
                the partial report and the failed/skipped DNs.
        """
        from niwaki.design import _push
        from niwaki.facade import AsyncNiwaki

        root = self._node.root()
        if isinstance(client, AsyncNiwaki):
            return _push.push_async(root, client, mode)
        return _push.push_sync(root, client, mode)


def _attach(
    parent: DesignNode,
    cls: type[ManagedObject],
    label: str,
    naming: dict[str, Any],
    attrs: dict[str, Any],
    *,
    position: str | None = None,
) -> Cursor:
    """Validate, create, and attach a child node; return its cursor.

    Raises:
        DesignError: Containment violation (*cls* is not a valid child).
        DuplicateDeclarationError: Same class + naming already declared here.
        pydantic.ValidationError: Naming/attribute constraint violation.
    """
    if cls._aci_class not in parent.cls._contains:  # pyright: ignore[reportPrivateUsage]
        raise DesignError(
            f"{cls.__name__} is not a valid APIC child of {parent.path()} ({parent.aci_class})."
        )
    _validate_attr_names(cls, attrs)
    node = DesignNode(cls, label, naming, attrs, parent, position=position)
    rn = node.rn  # constructs + validates the MO
    if any(sibling.aci_class == node.aci_class and sibling.rn == rn for sibling in parent.children):
        raise DuplicateDeclarationError(
            f"{node.path()} is already declared. Each object is declared "
            "exactly once; use set() on the original cursor to add attributes."
        )
    parent.children.append(node)
    return _cursor_class_for(node.position)(node)


def _child_position(owner: DesignNode, label: str) -> str | None:
    """Position of a curated child created by maker *label* on *owner*.

    ``None`` propagates: a maker invoked under an uncurated node (``.mo()``
    escape) yields an uncurated child.
    """
    if owner.position is None:
        return None
    return f"{owner.position}.{label}" if owner.position else label


def _make_child(
    owner: DesignNode,
    label: str,
    child_aci_class: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Cursor:
    """Maker implementation shared by all dynamically dispatched makers."""
    cls = _load_class(child_aci_class)
    naming, attrs = _split_naming(cls, args, kwargs)
    attrs = apply_sugar(child_aci_class, attrs)
    return _attach(owner, cls, label, naming, attrs, position=_child_position(owner, label))
