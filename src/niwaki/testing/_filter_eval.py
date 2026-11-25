"""Parse and evaluate the APIC filter grammar the SDK renders.

``query-target-filter`` and ``rsp-subtree-filter`` carry expressions such as::

    and(wcard(fvBD.name,"prod-*"),eq(fvBD.descr,"say \\"hi\\""))

This module parses that text into a small tree (fail-loud on anything
malformed or outside the grammar) and compiles it into a predicate over
stored objects.  Comparisons are semantic, through :mod:`._semantics`: a
named number matches by value, a bitmask as a set, a boolean whatever its
spelling, a number numerically.

The grammar covered is exactly what :mod:`niwaki.query._filters` renders —
``eq ne lt le gt ge wcard bw anybit`` on a qualified property, and the
combinators ``and or xor not``.  Every other operator is refused by name:
the APIC has no ``allbit`` (the SDK emulates it with ``anybit``), and a
``raw()`` operator the fake cannot evaluate must never match silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from niwaki.testing._semantics import DN, Prop, Refusal, expand_class, prop

#: The view of a stored object a predicate reads: ``(dn, class, attributes)``.
type Subject = tuple[str, str, Mapping[str, str]]
type Predicate = Callable[[Subject], bool]

_COMPARISONS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})
_COMBINATORS = frozenset({"and", "or", "xor"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass(frozen=True, slots=True)
class _Call:
    op: str
    args: tuple[_Node, ...]


@dataclass(frozen=True, slots=True)
class _Ref:
    """A bare identifier: a ``class.prop`` reference."""

    name: str


@dataclass(frozen=True, slots=True)
class _Str:
    value: str


type _Node = _Call | _Ref | _Str


# ── Parsing ───────────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, detail: str) -> Refusal:
        return Refusal(f"malformed filter {self.text!r}: {detail} at offset {self.pos}")

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def node(self) -> _Node:
        self.skip()
        if self.pos >= len(self.text):
            raise self.fail("unexpected end")
        if self.text[self.pos] == '"':
            return _Str(self.string())
        match = _IDENT.match(self.text, self.pos)
        if match is None:
            raise self.fail(f"unexpected {self.text[self.pos]!r}")
        self.pos = match.end()
        self.skip()
        if self.pos < len(self.text) and self.text[self.pos] == "(":
            self.pos += 1
            return _Call(match.group(), self.arguments())
        return _Ref(match.group())

    def arguments(self) -> tuple[_Node, ...]:
        args: list[_Node] = []
        self.skip()
        if self.pos < len(self.text) and self.text[self.pos] == ")":
            self.pos += 1
            return ()
        while True:
            args.append(self.node())
            self.skip()
            if self.pos >= len(self.text):
                raise self.fail("unclosed parenthesis")
            char = self.text[self.pos]
            self.pos += 1
            if char == ")":
                return tuple(args)
            if char != ",":
                raise self.fail(f"expected ',' or ')', got {char!r}")

    def string(self) -> str:
        self.pos += 1  # opening quote
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                if self.pos + 1 >= len(self.text):
                    break
                out.append(self.text[self.pos + 1])
                self.pos += 2
                continue
            self.pos += 1
            if char == '"':
                return "".join(out)
            out.append(char)
        raise self.fail("unterminated string")


def _parse(text: str) -> _Node:
    parser = _Parser(text)
    root = parser.node()
    parser.skip()
    if parser.pos != len(text):
        raise parser.fail("trailing content")
    if not isinstance(root, _Call):
        raise Refusal(f"malformed filter {text!r}: not an operator expression")
    return root


# ── Compilation ───────────────────────────────────────────────────────────────


def compile_filter(text: str, scope: frozenset[str] | None = None) -> Predicate:
    """Compile an APIC filter string into a predicate over stored objects.

    Everything that can be checked without data is checked here, before a
    single object is read: the syntax, every operator and arity, every
    ``class.prop`` reference, the orderings, wildcards and literals the fake
    cannot honour — and every term's class against *scope*.  So a bad filter
    fails loud even against an empty store.

    A term ``op(X.prop, …)`` applies to ``X`` and its concrete subclasses.
    What the APIC does with a term met by an object of another class was never
    measured, so the returned predicate refuses such an object rather than
    guess (``ne`` and ``not(eq)`` would otherwise disagree).

    Args:
        text: The filter, as sent on the wire (already URL-decoded).
        scope: The classes the filter can meet (the query class, or the
            target class list — concrete subclasses included), or ``None``
            when the request does not bound them.

    Returns:
        A predicate taking ``(dn, class, attributes)``; it raises
        :class:`Refusal` on an object some term does not apply to.

    Raises:
        Refusal: The filter is malformed, uses an operator outside the
            grammar, names an unknown class or property, asks for what the
            fake cannot honour, or has a term no class in *scope* can meet.
    """
    terms: list[Prop] = []
    predicate = _compile(_parse(text), text, terms)
    reaches: list[tuple[Prop, frozenset[str]]] = []
    for term in terms:
        applies = expand_class(term.aci_class)
        if scope is not None and not applies & scope:
            raise Refusal(
                f"filter term on {term.qualified} cannot meet the classes this request returns"
            )
        reaches.append((term, applies))

    def checked(subject: Subject) -> bool:
        dn, aci_class, _ = subject
        for term, applies in reaches:
            if aci_class not in applies:
                raise Refusal(
                    f"filter term on {term.qualified} met by the {aci_class} {dn}: what the "
                    "APIC answers for a term qualified with another class is not known"
                )
        return predicate(subject)

    return checked


def _compile(node: _Node, text: str, terms: list[Prop]) -> Predicate:
    if not isinstance(node, _Call):
        raise Refusal(f"malformed filter {text!r}: expected an operator expression")
    op, args = node.op, node.args
    if op in _COMBINATORS or op == "not":
        parts = [_compile(arg, text, terms) for arg in args]
        if op == "not":
            if len(parts) != 1:
                raise Refusal(f"malformed filter {text!r}: not() takes one expression")
            (inner,) = parts
            return lambda subject: not inner(subject)
        if not parts:
            raise Refusal(f"malformed filter {text!r}: {op}() needs an expression")
        if op == "and":
            return lambda subject: all(part(subject) for part in parts)
        if op == "or":
            return lambda subject: any(part(subject) for part in parts)
        return lambda subject: sum(part(subject) for part in parts) % 2 == 1
    if op in _COMPARISONS or op in ("wcard", "anybit"):
        target, (value,) = _operands(node, text, 1)
        terms.append(target)
        return _atom(target, _comparison(op, target, value))
    if op == "bw":
        target, (low, high) = _operands(node, text, 2)
        target.check_orderable()
        terms.append(target)
        return _atom(
            target,
            lambda stored: target.order(stored, low) >= 0 and target.order(stored, high) <= 0,
        )
    raise Refusal(f"filter operator {op!r} is not in the APIC filter grammar ({text!r})")


def _operands(node: _Call, text: str, count: int) -> tuple[Prop, tuple[str, ...]]:
    """Check ``op(class.prop,"v1"[,"v2"])`` and resolve its property."""
    args = node.args
    if len(args) != count + 1 or not isinstance(args[0], _Ref):
        raise Refusal(
            f"malformed filter {text!r}: {node.op}() takes a class.prop and {count} string(s)"
        )
    values: list[str] = []
    for arg in args[1:]:
        if not isinstance(arg, _Str):
            raise Refusal(f"malformed filter {text!r}: {node.op}() values must be quoted strings")
        values.append(arg.value)
    qualified = args[0].name
    aci_class, dot, wire = qualified.partition(".")
    if not dot or not aci_class or not wire or "." in wire:
        raise Refusal(f"filter property {qualified!r} must be qualified as class.prop")
    return prop(aci_class, wire), tuple(values)


def _comparison(op: str, target: Prop, literal: str) -> Callable[[str], bool]:
    match op:
        case "eq":
            target.check_literal(literal)
            return lambda stored: target.equal(stored, literal)
        case "ne":
            target.check_literal(literal)
            return lambda stored: not target.equal(stored, literal)
        case "lt":
            target.check_orderable()
            return lambda stored: target.order(stored, literal) < 0
        case "le":
            target.check_orderable()
            return lambda stored: target.order(stored, literal) <= 0
        case "gt":
            target.check_orderable()
            return lambda stored: target.order(stored, literal) > 0
        case "ge":
            target.check_orderable()
            return lambda stored: target.order(stored, literal) >= 0
        case "anybit":
            if target.kind != "flags":
                raise Refusal(f"anybit() on {target.qualified}, which is not a bitmask")
            return lambda stored: target.matches_any_bit(stored, literal)
        case _:  # wcard — a substring match where a trailing '*' spans anything
            target.check_wildcard(literal)
            pattern = re.compile(".*".join(re.escape(part) for part in literal.split("*")))
            return lambda stored: pattern.search(stored) is not None


def _atom(target: Prop, test: Callable[[str], bool]) -> Predicate:
    """Bind a value test to a property (applicability is checked by the caller)."""

    def predicate(subject: Subject) -> bool:
        dn, _, attributes = subject
        stored = dn if target.wire == DN else attributes.get(target.wire, target.default)
        return test(stored)

    return predicate
