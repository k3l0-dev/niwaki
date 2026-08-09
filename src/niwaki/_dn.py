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
"""

from __future__ import annotations

from typing import NamedTuple


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


def naming_values(rn: str, rn_format: str) -> dict[str, str]:
    """Extract the naming property values an RN carries, given its format.

    Walks *rn_format* and *rn* in lockstep so that several values separated by
    literals are each recovered, and a bracketed value is scanned to its
    *matching* close bracket rather than the first one — the two cases a single
    regex cannot cover at once.

    Args:
        rn: One RN segment, e.g. ``subnet-[10.0.1.1/24]``.
        rn_format: The class's RN format, e.g. ``subnet-[{ip}]``.

    Returns:
        A mapping of naming property name to its wire value.  Empty when the
        format has no placeholders (a fixed RN such as ``rsctx``).

    Raises:
        ValueError: *rn* does not match *rn_format* — a literal did not line up,
            or a bracket did not close.  A DN the SDK cannot parse is a fail-loud
            event, never a silent partial result.

    Example::

        naming_values("subnet-[10.0.1.1/24]", "subnet-[{ip}]")
        # → {"ip": "10.0.1.1/24"}
        naming_values("iprule-[a/b]-dom-common", "iprule-[{objectDn}]-dom-{domain}")
        # → {"objectDn": "a/b", "domain": "common"}
    """
    tokens = _tokenize_rn_format(rn_format)
    values: dict[str, str] = {}
    pos = 0
    for index, (kind, text) in enumerate(tokens):
        if kind == "lit":
            if not rn.startswith(text, pos):
                raise ValueError(f"RN {rn!r} does not match format {rn_format!r} at {text!r}")
            pos += len(text)
            continue
        # A property value. If the preceding literal ended in "[", the value is
        # bracketed: scan to the matching "]" by bracket depth. Otherwise it runs
        # to the start of the next literal (or the end of the RN).
        prev = tokens[index - 1] if index else ("lit", "")
        if prev[0] == "lit" and prev[1].endswith("["):
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
                raise ValueError(f"RN {rn!r} has an unterminated bracket for {text!r}")
            values[text] = rn[pos:end]
            pos = end
        else:
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is None:
                values[text] = rn[pos:]
                pos = len(rn)
            else:
                sep = nxt[1]
                end = rn.find(sep, pos)
                if end < 0:
                    raise ValueError(f"RN {rn!r} missing separator {sep!r} after {text!r}")
                values[text] = rn[pos:end]
                pos = end
    if pos != len(rn):
        raise ValueError(f"RN {rn!r} has trailing content past format {rn_format!r}")
    return values


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


def parse(dn: str, rn_format: str) -> DnParts:
    """Decompose a DN into parent, RN, and naming values, given the RN format.

    The RN-format-agnostic entry point: the caller supplies the format (from a
    model's ``_rn_format`` or the catalogue's ``rn_format`` column), so this
    function has no dependency on the catalogue or on ACI.

    Args:
        dn: The Distinguished Name to decompose.
        rn_format: The RN format of *dn*'s own class.

    Returns:
        A :class:`DnParts`.

    Raises:
        ValueError: The DN's last segment does not match *rn_format*.

    Example::

        parse("uni/tn-p/BD-web", "BD-{name}")
        # → DnParts(parent="uni/tn-p", rn="BD-web", naming={"name": "web"})
    """
    rn = rn_of(dn)
    return DnParts(parent=parent_dn(dn), rn=rn, naming=naming_values(rn, rn_format))
