"""Parse an APIC Distinguished Name back into its parts — the inverse of naming.

The SDK has always been able to *compute* a DN: a model fills the ``{prop}``
placeholders of its RN format with wire values (``fvBD(name="web").dn`` →
``uni/tn-prod/BD-web``).  Reading a fabric back needs the inverse — given a DN,
recover the parent DN and the naming values — and that is what this module does.

It is deliberately **table-driven and ACI-agnostic**: the core functions take an
RN *format string* and know nothing about which management model produced it.
The only ACI-specific step, resolving a class name to its ``rn_format``, lives in
one thin wrapper that reads the shipped catalogue.  A second management model
built on the same DME shape (NX-OS) would reuse the core untouched.

Two subtleties make a naive ``str.split("/")`` or a single regex wrong, and both
are handled here:

- **Bracketed naming values contain anything, including slashes and *nested*
  brackets.**  ``rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]`` is one
  RN segment carrying one value, not five.  Splitting is bracket-depth aware.
- **An RN can carry several naming props with literal separators between them.**
  ``iprule-[{objectDn}]-dom-{domain}-sourcerule-[{partialRuleDn}]`` interleaves
  three values and two literals.  Extraction walks template and RN in lockstep
  rather than matching one greedy group.

A third subtlety cannot be handled by the format alone: an **unbracketed value
may contain the literal that follows it**.  A leaf selector named ``a-typ-b``
of type ``range`` has the RN ``leaves-a-typ-b-typ-range`` under the format
``leaves-{name}-typ-{type}`` — and that RN reads just as well as ``name="a"``,
``type="b-typ-range"``.  Stopping at the first occurrence of the separator would
silently corrupt the identity, so :func:`naming_values` enumerates *every*
structural parse instead:

- exactly one parse → the answer (every RN without such a collision);
- several parses → an optional ``accept(prop, value)`` predicate, supplied by
  the caller who knows what each property may hold (an enum member, an
  integer…), filters them; what survives must be unique;
- several admissible parses → :class:`AmbiguousRnError`, listing them.  The
  parser never guesses.

The enumeration is bounded by construction: a state (placeholder, position)
proven a dead end is never explored twice, and it stops once
:data:`MAX_CANDIDATES` parses plus one are known — an adversarial RN with
hundreds of thousands of parses costs milliseconds, not seconds, and its error
stays small.  (A bracketed value is matched by depth; an RN whose bracketed
value is itself unbalanced — which no fabric produces — is not parsed.)
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import NamedTuple

type Accept = Callable[[str, str], bool]
"""``accept(prop, value)`` — whether *value* is admissible for naming *prop*."""

MAX_CANDIDATES = 64
"""The most parses an :class:`AmbiguousRnError` carries (the enumeration stops there)."""

_LISTED = 5
"""The most candidates an :class:`AmbiguousRnError` message spells out."""


class AmbiguousRnError(ValueError):
    """An RN admits several parses under its format, and nothing settles them.

    Raised by :func:`naming_values` (and :func:`parse`) when an unbracketed
    naming value may stop at more than one occurrence of the literal that
    follows it, and the caller's ``accept`` predicate — if any — leaves more
    than one parse standing.  A subclass of :class:`ValueError`, so every
    caller that already treats an unparseable RN as a failure keeps doing so.

    Attributes:
        rn: The RN segment that was parsed.
        rn_format: The RN format it was parsed against.
        candidates: The admissible parses (at least two, at most
            :data:`MAX_CANDIDATES`), in a deterministic order: sorted by their
            values, taken in the format's placeholder order.
        truncated: ``True`` when more parses exist than *candidates* holds —
            the enumeration stopped at :data:`MAX_CANDIDATES`, so the listed
            parses are the first ones in that order, not all of them.

    Example::

        try:
            naming_values("leaves-a-typ-b-typ-range", "leaves-{name}-typ-{type}")
        except AmbiguousRnError as exc:
            exc.candidates
            # → ({"name": "a", "type": "b-typ-range"},
            #    {"name": "a-typ-b", "type": "range"})
    """

    def __init__(
        self,
        rn: str,
        rn_format: str,
        candidates: tuple[dict[str, str], ...],
        *,
        truncated: bool = False,
    ) -> None:
        self.rn = rn
        self.rn_format = rn_format
        self.candidates = candidates
        self.truncated = truncated
        count = f"more than {len(candidates)}" if truncated else str(len(candidates))
        listed = "; ".join(repr(candidate) for candidate in candidates[:_LISTED])
        more = ", …" if truncated or len(candidates) > _LISTED else ""
        super().__init__(
            f"RN {rn!r} is ambiguous under format {rn_format!r}: "
            f"{count} parses fit ({listed}{more})"
        )


def split_dn(dn: str) -> list[str]:
    """Split a DN into its RN segments, respecting bracketed values.

    A ``/`` only separates segments at bracket depth zero: a slash inside a
    ``[...]`` naming value (an IP prefix, a nested path DN) is part of the value.

    Args:
        dn: The Distinguished Name, e.g. ``uni/tn-p/BD-w/subnet-[10.0.1.1/24]``.

    Returns:
        The RN segments in order, e.g. ``["uni", "tn-p", "BD-w",
        "subnet-[10.0.1.1/24]"]``.  An empty string yields ``[]``.

    Example::

        split_dn("uni/tn-p/BD-w/subnet-[10.0.1.1/24]")
        # → ["uni", "tn-p", "BD-w", "subnet-[10.0.1.1/24]"]
    """
    if not dn:
        return []
    segments: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(dn):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "/" and depth == 0:
            segments.append(dn[start:i])
            start = i + 1
    segments.append(dn[start:])
    return segments


def rn_of(dn: str) -> str:
    """Return the object's own RN — the last bracket-aware segment of its DN.

    Args:
        dn: The Distinguished Name.

    Returns:
        The final RN segment, e.g. ``subnet-[10.0.1.1/24]``.  For a top-level
        DN with no separator (``uni``) this is the DN itself.

    Example::

        rn_of("uni/tn-p/BD-w")  # → "BD-w"
    """
    return split_dn(dn)[-1] if dn else ""


def parent_dn(dn: str) -> str | None:
    """Return the DN of the object's parent, or ``None`` if it has none.

    The parent is the DN with its last RN segment removed.  A top-level object
    (a DN of a single segment, such as ``uni`` or ``topology``) has no parent.

    Args:
        dn: The Distinguished Name.

    Returns:
        The parent DN, or ``None`` when *dn* is a single segment or empty.

    Example::

        parent_dn("uni/tn-p/BD-w")               # → "uni/tn-p"
        parent_dn("uni/tn-p/subnet-[10/8]")      # → "uni/tn-p"
        parent_dn("uni")                          # → None
    """
    segments = split_dn(dn)
    if len(segments) <= 1:
        return None
    return "/".join(segments[:-1])


def _tokenize_rn_format(rn_format: str) -> list[tuple[str, str]]:
    """Break an RN format into ``("lit", text)`` and ``("prop", name)`` tokens.

    ``BD-{name}`` → ``[("lit", "BD-"), ("prop", "name")]``.
    ``subnet-[{ip}]`` → ``[("lit", "subnet-["), ("prop", "ip"), ("lit", "]")]``.

    The surrounding brackets stay in the literal tokens, which is what lets the
    lockstep matcher below recognise a bracketed value and scan to its matching
    close.
    """
    tokens: list[tuple[str, str]] = []
    literal = ""
    i = 0
    while i < len(rn_format):
        char = rn_format[i]
        if char == "{":
            end = rn_format.index("}", i)
            if literal:
                tokens.append(("lit", literal))
                literal = ""
            tokens.append(("prop", rn_format[i + 1 : end]))
            i = end + 1
        else:
            literal += char
            i += 1
    if literal:
        tokens.append(("lit", literal))
    return tokens


@cache
def _rn_tokens(rn_format: str) -> tuple[tuple[str, str], ...]:
    """Memoised :func:`_tokenize_rn_format` — a scope walk tries ~13,500 formats."""
    return tuple(_tokenize_rn_format(rn_format))


@cache
def _placeholders(rn_format: str) -> tuple[str, ...]:
    """The format's placeholder names, in order (a repeated one appears twice)."""
    return tuple(text for kind, text in _rn_tokens(rn_format) if kind == "prop")


def _structural_parses(
    rn: str,
    rn_format: str,
    accept: Accept | None,
    *,
    limit: int,
) -> tuple[list[dict[str, str]], str | None]:
    """Enumerate up to *limit* parses of *rn*; return them with the first dead end met.

    Depth-first, earliest split first, which yields the parses already sorted
    by their values in placeholder order (at each branch the shorter value is
    a prefix of the longer one).  The first dead end recorded is the one on
    the first-occurrence path — the failure the historical single-pass parser
    reported — so a structurally invalid RN keeps its error message.

    *accept* prunes a branch as soon as a value is fixed.  A ``(token, pos)``
    state that produced no parse is remembered and never re-entered, which
    keeps a refusing *accept* polynomial; that memo is only sound when no
    placeholder repeats (a repeated one makes the state value-dependent), so
    it is skipped for such formats.
    """
    tokens = _rn_tokens(rn_format)
    props = _placeholders(rn_format)
    memoise = len(set(props)) == len(props)
    parses: list[dict[str, str]] = []
    errors: list[str] = []
    values: dict[str, str] = {}
    verdicts: dict[tuple[str, str], bool] = {}
    dead: set[tuple[int, int]] = set()

    def admissible(prop: str, value: str) -> bool:
        if accept is None:
            return True
        key = (prop, value)
        verdict = verdicts.get(key)
        if verdict is None:
            verdict = verdicts[key] = accept(prop, value)
        return verdict

    def walk(index: int, pos: int) -> None:
        if index == len(tokens):
            if pos == len(rn):
                parses.append(dict(values))
            elif not errors:
                errors.append(f"RN {rn!r} has trailing content past format {rn_format!r}")
            return
        kind, text = tokens[index]
        if kind == "lit":
            if rn.startswith(text, pos):
                walk(index + 1, pos + len(text))
            elif not errors:
                errors.append(f"RN {rn!r} does not match format {rn_format!r} at {text!r}")
            return
        if memoise and (index, pos) in dead:
            return
        found_before = len(parses)
        prev = tokens[index - 1] if index else ("lit", "")
        if prev[0] == "lit" and prev[1].endswith("["):
            # Bracketed: matched by depth — one deterministic end.
            depth = 1
            end = pos
            while end < len(rn) and depth:
                if rn[end] == "[":
                    depth += 1
                elif rn[end] == "]":
                    depth -= 1
                if depth:
                    end += 1
            if depth:
                if not errors:
                    errors.append(f"RN {rn!r} has an unterminated bracket for {text!r}")
                return
            ends = [end]
        elif index + 1 == len(tokens):
            ends = [len(rn)]  # the last value runs to the end of the RN
        else:
            # Unbracketed, followed by a literal: any occurrence may close it.
            sep = tokens[index + 1][1]
            ends = []
            found = rn.find(sep, pos)
            while found >= 0:
                ends.append(found)
                found = rn.find(sep, found + 1)
            if not ends:
                if not errors:
                    errors.append(f"RN {rn!r} missing separator {sep!r} after {text!r}")
                return
        prior = values.get(text)
        for end in ends:
            if len(parses) >= limit:
                return
            value = rn[pos:end]
            if prior is not None and value != prior:
                continue  # a repeated placeholder carries one value
            if not admissible(text, value):
                continue
            values[text] = value
            walk(index + 1, end)
            if prior is None:
                del values[text]
        if len(parses) == found_before:
            dead.add((index, pos))

    walk(0, 0)
    return parses, errors[0] if errors else None


def _has_parse(rn: str, rn_format: str) -> bool:
    """Whether *rn* fits *rn_format* at all — ambiguously or not, stopping at one parse.

    The probe a class resolver needs: it answers "could this segment belong
    to this class?" without paying for the enumeration.
    """
    tokens = _rn_tokens(rn_format)
    if tokens and tokens[0][0] == "lit" and not rn.startswith(tokens[0][1]):
        return False
    return bool(_structural_parses(rn, rn_format, None, limit=1)[0])


def naming_values(rn: str, rn_format: str, *, accept: Accept | None = None) -> dict[str, str]:
    """Extract the naming property values an RN carries, given its format.

    Walks *rn_format* and *rn* in lockstep so that several values separated by
    literals are each recovered, and a bracketed value is scanned to its
    *matching* close bracket rather than the first one — the two cases a single
    regex cannot cover at once.

    An unbracketed value followed by a literal may contain that literal
    (``leaves-a-typ-b-typ-range`` under ``leaves-{name}-typ-{type}``), so every
    occurrence of the literal is tried as the value's end and **every**
    structural parse is enumerated.  A bracketed value stays matched by depth
    and the last unbracketed value runs to the end of the RN — neither ever
    branches.  An empty value is structurally legal, and a placeholder the
    format repeats must carry the same value at each occurrence.  The
    enumeration stops past :data:`MAX_CANDIDATES` parses (the ambiguity is
    then reported ``truncated``), so an adversarial RN stays cheap.

    Args:
        rn: One RN segment, e.g. ``subnet-[10.0.1.1/24]``.
        rn_format: The class's RN format, e.g. ``subnet-[{ip}]``.
        accept: Optional ``accept(prop, value)`` predicate, called for each
            candidate value (bracketed or not; once per distinct pair).  A
            parse survives only if every one of its values is accepted.  This
            is where a caller that knows the properties (an enum, an integer)
            settles a structural ambiguity; the parser itself stays agnostic.
            ``None`` accepts everything.

    Returns:
        A mapping of naming property name to its wire value.  Empty when the
        format has no placeholders (a fixed RN such as ``rsctx``).

    Raises:
        AmbiguousRnError: Two or more admissible parses — the RN alone cannot
            say which identity it carries.  Never resolved by guessing; at
            most :data:`MAX_CANDIDATES` of them are carried.
        ValueError: *rn* does not match *rn_format* — a literal did not line up
            ("does not match"), a bracket did not close ("unterminated
            bracket"), a separator is absent ("missing separator"), or content
            is left over ("trailing content") — or parses exist but *accept*
            refuses them all ("no admissible").  A DN the SDK cannot parse is
            a fail-loud event, never a silent partial result.

    Example::

        naming_values("subnet-[10.0.1.1/24]", "subnet-[{ip}]")
        # → {"ip": "10.0.1.1/24"}
        naming_values("iprule-[a/b]-dom-common", "iprule-[{objectDn}]-dom-{domain}")
        # → {"objectDn": "a/b", "domain": "common"}
        naming_values(
            "leaves-a-typ-b-typ-range",
            "leaves-{name}-typ-{type}",
            accept=lambda prop, value: prop != "type" or value in {"ALL", "range"},
        )
        # → {"name": "a-typ-b", "type": "range"}
    """
    tokens = _rn_tokens(rn_format)
    if tokens and tokens[0][0] == "lit" and not rn.startswith(tokens[0][1]):
        # The overwhelmingly common miss (a scope walk tries every catalogued
        # format against one segment): refused before any enumeration.
        raise ValueError(f"RN {rn!r} does not match format {rn_format!r} at {tokens[0][1]!r}")
    parses, error = _structural_parses(rn, rn_format, accept, limit=MAX_CANDIDATES + 1)
    if len(parses) == 1:
        return parses[0]
    if parses:
        props = _placeholders(rn_format)
        ordered = sorted(parses, key=lambda parse: tuple(parse[prop] for prop in props))
        raise AmbiguousRnError(
            rn,
            rn_format,
            tuple(ordered[:MAX_CANDIDATES]),
            truncated=len(ordered) > MAX_CANDIDATES,
        )
    if accept is not None:
        structural, error = _structural_parses(rn, rn_format, None, limit=1)
        if structural:
            raise ValueError(
                f"RN {rn!r} has no admissible parse under format {rn_format!r}: "
                "every structural parse holds a value its property refuses"
            )
    # No parse at all: every branch hit a dead end, and the first one met is
    # the historical single-pass failure.
    raise ValueError(error or f"RN {rn!r} does not match format {rn_format!r}")


class DnParts(NamedTuple):
    """The pieces a DN decomposes into for tree reconstruction.

    Attributes:
        parent: The parent object's DN, or ``None`` for a top-level object.
        rn: This object's own RN (the DN's last segment).
        naming: Naming property → wire value, extracted from *rn*.
    """

    parent: str | None
    rn: str
    naming: dict[str, str]


def parse(dn: str, rn_format: str, *, accept: Accept | None = None) -> DnParts:
    """Decompose a DN into parent, RN, and naming values, given the RN format.

    The RN-format-agnostic entry point: the caller supplies the format (from a
    model's ``_rn_format`` or the catalogue's ``rn_format`` column), so this
    function has no dependency on the catalogue or on ACI.

    Args:
        dn: The Distinguished Name to decompose.
        rn_format: The RN format of *dn*'s own class.
        accept: Optional ``accept(prop, value)`` predicate, relayed to
            :func:`naming_values` — it settles an RN several parses fit.

    Returns:
        A :class:`DnParts`.

    Raises:
        AmbiguousRnError: The DN's last segment admits several admissible
            parses (see :func:`naming_values`).
        ValueError: The DN's last segment does not match *rn_format*, or no
            parse of it is admissible.

    Example::

        parse("uni/tn-p/BD-web", "BD-{name}")
        # → DnParts(parent="uni/tn-p", rn="BD-web", naming={"name": "web"})
    """
    rn = rn_of(dn)
    return DnParts(parent=parent_dn(dn), rn=rn, naming=naming_values(rn, rn_format, accept=accept))
