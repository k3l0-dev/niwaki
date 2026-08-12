"""``to_code`` — emit executable DSL source from a design (2.0 it.4 lot A).

The last mile of the brownfield story: a reverse-imported (or hand-built)
design becomes **Python source** that replays it — reviewable, diffable,
versionable code instead of an opaque object.  The emitter consumes a
:class:`~niwaki.design.DesignView` (never a snapshot — the scoping decision:
code is the one artifact that ages in the user's repo, so it speaks the
DSL, not the wire).

The acceptance contract is mechanical: executing the emitted source
produces a design whose ``to_payload()`` is byte-identical (canonically) to
the source design's.  Everything the view carries is rendered — curated
makers with their naming and attributes, ``set()`` merges, every reference
(``bind``/``bind_dn``/verbs, with :func:`~niwaki.design.ref` when the
relation carries configuration), and the wire-name escape hatches
(``raw()``/``raw_set()``) — including the tag/annotation classes every
Terraform/Ansible-touched fabric carries.
"""

from __future__ import annotations

import keyword
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from niwaki.design._cursor import Cursor
    from niwaki.design._view import DesignView, DesignViewBind, DesignViewNode

#: Verbs with first-class methods on every cursor; every other verb is a
#: generated method on the position's typed cursor — both emit identically.
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(label: str, primary: str) -> str:
    base = f"{label}_{primary}" if primary else label
    slug = _SLUG_RE.sub("_", base.lower()).strip("_") or "node"
    if slug[0].isdigit():
        slug = f"n_{slug}"
    if keyword.iskeyword(slug):
        slug = f"{slug}_"
    return slug


def _literal(value: Any) -> str:
    """A replayable literal for one declared value.

    Values from an import are wire strings (replayed verbatim — the DSL's
    validators coerce).  Hand-declared values may be enums, sets or other
    typed spellings whose ``repr`` is not importable source — those render
    through the wire boundary (``to_wire``), which every field accepts back.
    """
    from niwaki.models._wire import to_wire

    if value is None or type(value) in (str, bool, int, float):
        # Exact types only: a StrEnum IS a str but reprs as
        # <Enum.member: 'x'> — unimportable source; it renders via to_wire.
        return repr(value)
    return repr(to_wire(value))


def _kwargs(attrs: dict[str, Any]) -> str:
    return ", ".join(f"{name}={_literal(value)}" for name, value in attrs.items())


def _wire_kwargs(attrs: dict[str, Any]) -> str:
    """Wire-named keyword arguments — via ``**{...}`` when a name needs it."""
    if all(name.isidentifier() and not keyword.iskeyword(name) for name in attrs):
        return _kwargs(attrs)
    inner = ", ".join(f"{name!r}: {_literal(value)}" for name, value in attrs.items())
    return f"**{{{inner}}}"


class _Emitter:
    def __init__(self, root_var: str) -> None:
        self.root_var = root_var
        self.lines: list[str] = []
        self.names: set[str] = {root_var}
        self.uses_ref = False
        self.unreplayable: list[str] = []

    def _check_replayable(self, node: DesignViewNode) -> None:
        """Collect what the public doors cannot replay (fail-loud, never emit).

        A design imported with ``on_unknown="raw"`` can carry classes and
        properties outside the shipped catalogue on the bypass channel; the
        emitted source only speaks the public API, whose doors validate
        against the catalogue — emitting them would produce source that
        crashes at exec.
        """
        from niwaki.query._catalog import catalog

        try:
            meta = catalog().class_meta(node.aci_class)
        except KeyError:
            self.unreplayable.append(f"{node.dn}: class {node.aci_class!r} is not in the catalogue")
            return
        for wire in node.raw_attrs:
            if wire not in meta.wire_to_readable:
                self.unreplayable.append(
                    f"{node.dn}: property {wire!r} is not a {node.aci_class} property"
                )

    def var_for(self, node: DesignViewNode) -> str:
        candidate = _slug(node.label, next(iter(node.naming.values()), ""))
        name, counter = candidate, 1
        while name in self.names:
            counter += 1
            name = f"{candidate}_{counter}"
        self.names.add(name)
        return name

    # ── Rendering pieces ──────────────────────────────────────────────────────

    def _readable_to_wire(self, node: DesignViewNode) -> dict[str, str]:
        """Field → wire-name map for a typed node emitted through ``raw()``."""
        from niwaki.design._cursor import _load_class

        cls = _load_class(node.aci_class)
        out: dict[str, str] = {}
        for field, info in cls.model_fields.items():
            alias = info.serialization_alias
            out[field] = alias if isinstance(alias, str) else field
        return out

    def _call_for(self, node: DesignViewNode, parent_var: str) -> str:
        """The creation expression for one node (no trailing refs)."""
        if node.position is not None:
            from niwaki.design._cursor import _load_class

            # Positional naming in the class's own order — the view records
            # the caller's kwargs order, which dynamic dispatch never
            # normalised (a permuted emission would corrupt identity).
            props = _load_class(node.aci_class)._naming_props  # pyright: ignore[reportPrivateUsage]
            ordered = [node.naming[p] for p in props if p in node.naming]
            args = [_literal(v) for v in ordered]
            if node.attrs:
                args.append(_kwargs(node.attrs))
            return f"{parent_var}.{node.label}({', '.join(args)})"

        # Escape hatch: raw() replays any class by wire name — the typed
        # route engages automatically for generated classes.  A typed node's
        # readable names translate back to wire; a catalogue node is already
        # wire-spelled end to end.
        from niwaki.domain._child_map import CLASS_PKG

        if node.aci_class in CLASS_PKG:
            wire_of = self._readable_to_wire(node)
            wire_attrs = {wire_of.get(k, k): v for k, v in {**node.naming, **node.attrs}.items()}
        else:
            wire_attrs = {**node.naming, **node.attrs}
        rendered = _wire_kwargs(wire_attrs)
        return f"{parent_var}.raw({node.aci_class!r}{', ' if rendered else ''}{rendered})"

    def _ref_arg(self, bind: DesignViewBind) -> str:
        if bind.attrs:
            self.uses_ref = True
            return f"ref({bind.target!r}, {_kwargs(bind.attrs)})"
        return repr(bind.target)

    def _bind_calls(self, node: DesignViewNode) -> list[str]:
        calls: list[str] = []
        for bind in node.binds:
            arg = self._ref_arg(bind)
            if bind.kind == "verb":
                calls.append(f".{bind.alias}({arg})")
            elif bind.kind == "bind_dn":
                calls.append(f".bind_dn({bind.alias}={arg})")
            else:
                calls.append(f".bind({bind.alias}={arg})")
        return calls

    # ── Walk ──────────────────────────────────────────────────────────────────

    def emit_node(self, node: DesignViewNode, parent_var: str) -> None:
        self._check_replayable(node)
        expression = self._call_for(node, parent_var)
        raw_set = f".raw_set({_wire_kwargs(node.raw_attrs)})" if node.raw_attrs else ""
        binds = self._bind_calls(node)

        if node.children:
            var = self.var_for(node)
            self.lines.append(f"{var} = {expression}")
            for call in binds:
                self.lines.append(f"{var}{call}")
            if raw_set:
                self.lines.append(f"{var}{raw_set}")
            for child in node.children:
                self.emit_node(child, var)
        else:
            self.lines.append(f"{expression}{''.join(binds)}{raw_set}")

    def emit_root(self, root: DesignViewNode) -> None:
        self.lines.append(f"{self.root_var} = design()")
        if root.attrs:
            self.lines.append(f"{self.root_var}.set({_kwargs(root.attrs)})")
        if root.raw_attrs:
            self.lines.append(f"{self.root_var}.raw_set({_wire_kwargs(root.raw_attrs)})")
        for child in root.children:
            self.emit_node(child, self.root_var)


def to_code(source: Cursor | DesignView, *, var: str = "cfg") -> str:
    """Emit the Python DSL source that replays *source* — the code emitter.

    The inverse of executing a design script: any design — hand-built,
    reverse-imported (:func:`~niwaki.design.to_design`), composed
    (:meth:`~niwaki.design.Cursor.slice` / :func:`~niwaki.design.merge`) —
    renders as reviewable, replayable Python.  Executing the emitted source
    yields a design whose payload is canonically byte-identical to the
    source's; the acceptance suite pins exactly that round trip.

    Args:
        source: A design cursor (any cursor — the whole design is taken,
            like ``push``) or an already-taken
            :class:`~niwaki.design.DesignView`.
        var: Name of the root variable in the emitted source (default
            ``"cfg"``) — the emitted script ends with that variable holding
            the root cursor.

    Returns:
        Python source text: imports first, then the declarations in the
        design's own order.  Curated positions render as their makers and
        references; everything outside the curated vocabulary renders
        through the wire-name doors (``raw()`` / ``raw_set()``) — including
        the tag/annotation objects that fabrics touched by other tooling
        always carry.

    Example::

        from niwaki.design import tenant, to_code

        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="main")
        cfg.vrf("main")
        print(to_code(cfg))
        # cfg = design()
        # tenant_prod = cfg.tenant('prod')
        # tenant_prod.bd('web').bind(vrf='main')
        # tenant_prod.vrf('main')
    """
    from niwaki.design._cursor import Cursor

    if isinstance(source, Cursor):
        view: DesignView = source.view()
    else:
        view = source
    from niwaki.exceptions._design import DesignError

    if not var.isidentifier() or keyword.iskeyword(var) or var in ("design", "ref"):
        raise DesignError(
            f"to_code(): {var!r} is not usable as the root variable "
            "(identifier required; 'design' and 'ref' would shadow the imports)."
        )

    emitter = _Emitter(var)
    emitter.emit_root(view.root)
    if emitter.unreplayable:
        raise DesignError(
            f"to_code(): {len(emitter.unreplayable)} item(s) live on the "
            "wire-bypass channel (on_unknown='raw' import) and the public "
            "doors cannot replay them:\n  " + "\n  ".join(emitter.unreplayable)
        )
    imports = ["design"] + (["ref"] if emitter.uses_ref else [])
    header = f"from niwaki.design import {', '.join(imports)}"
    return "\n".join([header, "", *emitter.lines]) + "\n"
