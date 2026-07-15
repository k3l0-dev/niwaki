"""The ACI type map — every schema ``baseType``, and what it becomes in Python.

The APIC schemas describe each property with a ``baseType``.  There are **45**
distinct configurable ones across the 15,452 schemas, and this module maps every
single one of them, on purpose.

Why it exists at all: the extractor used to know six families and quietly fall
back to ``str`` for everything else.  That fallback is what typed
``bgpCtxPol.hold_interval`` (a ``Uint16``) as a string while
``lacpLagPol.max_links`` (a ``Uint32``) was an int — same semantics, different
type, purely by accident of which branch someone had written.  It is also what
made bitmasks unusable.  A silent fallback does not fail; it *degrades*, and a
degradation nobody sees is a bug that ships.

So: **an unknown ``baseType`` raises.**  A firmware that introduces a new family
breaks this repository's build, not a user's code.  ``str`` is still the right
answer for a DN, an encap or a password — but it is now a **decision**, written
here with its reason, never a default.

The map is the single source of truth for:

* what the extractor writes into ``sdk_subset.json``;
* what the generated models declare;
* what the guard suite re-derives from the raw schemas and demands, property by
  property, for all 2,222 classes.
"""

from __future__ import annotations

from enum import StrEnum


class UnknownBaseTypeError(KeyError):
    """A schema property uses a ``baseType`` this SDK has never classified.

    Raised by :func:`kind_for`.  It is deliberately fatal: the alternative is to
    guess, and guessing is what produced the typing this module exists to fix.
    """


class FieldKind(StrEnum):
    """What a schema ``baseType`` becomes on a generated model."""

    BOOL = "bool"
    """``scalar:Bool`` → ``bool`` (the APIC writes ``"true"``/``"false"``)."""

    ENUM = "enum"
    """One choice among a closed set → a generated ``StrEnum``."""

    FLAGS = "flags"
    """A *subset* of a closed set → ``frozenset[SomeEnum]``.

    An ACI bitmask is a set, not a string: the APIC stores it comma-joined and
    reorders it as it pleases (``"shared,public"`` is stored ``"public,shared"``,
    ascending bit weight).  Modelling it as a set is what makes that reordering
    invisible instead of an eternal false drift.
    """

    INT = "int"
    """An integer, with the range the schema's validators declare."""

    FLOAT = "float"
    """A real number."""

    STR = "str"
    """A string — by decision (see the map below), never by fallback."""

    IP = "ip"
    """A string validated as an IP address or prefix."""

    MAC = "mac"
    """A string validated as a MAC address."""


#: Every configurable ``baseType`` in the APIC 6.0 schema corpus.
#:
#: Grouped by decision, with the reason for each ``STR`` — those are the ones
#: that would otherwise look like an oversight.
BASETYPE_MAP: dict[str, FieldKind] = {
    # ── Booleans ─────────────────────────────────────────────────────────────
    "scalar:Bool": FieldKind.BOOL,
    # ── One choice among a closed set ─────────────────────────────────────────
    "scalar:Enum8": FieldKind.ENUM,
    "scalar:Enum16": FieldKind.ENUM,
    "scalar:Enum32": FieldKind.ENUM,
    "scalar:Enum64": FieldKind.ENUM,
    # ── A subset of a closed set (a bitmask) ──────────────────────────────────
    "scalar:Bitmask8": FieldKind.FLAGS,
    "scalar:Bitmask16": FieldKind.FLAGS,
    "scalar:Bitmask32": FieldKind.FLAGS,
    "scalar:Bitmask64": FieldKind.FLAGS,
    # ── Numbers ───────────────────────────────────────────────────────────────
    "scalar:UByte": FieldKind.INT,
    "scalar:Uint16": FieldKind.INT,
    "scalar:Uint32": FieldKind.INT,
    "scalar:Uint64": FieldKind.INT,
    "scalar:SByte": FieldKind.INT,
    "scalar:Sint16": FieldKind.INT,
    "scalar:Sint32": FieldKind.INT,
    "scalar:Sint64": FieldKind.INT,
    "scalar:Seconds": FieldKind.INT,
    "scalar:Float": FieldKind.FLOAT,
    "scalar:Double": FieldKind.FLOAT,
    # ── Addresses (a string, but a validated one) ─────────────────────────────
    "address:Ip": FieldKind.IP,
    "address:IPv4": FieldKind.IP,  # generic IP regex accepts v4 and v6 alike
    "address:IPv6": FieldKind.IP,
    "address:MAC": FieldKind.MAC,
    # MACPrefix is a misnomer: its five props are Fibre Channel identifiers
    # (fcId, fcMap — "0E:FC:00", a 3-octet FC-MAP), not MAC addresses.  The
    # 6-octet MAC regex would reject every legitimate value, so this is a
    # validated-by-the-APIC string, on purpose.
    "address:MACPrefix": FieldKind.STR,
    # ── Strings, by decision ──────────────────────────────────────────────────
    "string:Basic": FieldKind.STR,  # the schema carries length + regex
    "string:CharBuffer": FieldKind.STR,  # free text (descriptions, banners)
    "string:Password": FieldKind.STR,  # write-only; the APIC never echoes it
    "reference:BinRef": FieldKind.STR,  # a DN — an opaque path, not a value
    "base:Encap": FieldKind.STR,  # "vlan-100", "vxlan-9000" — a tagged scalar
    "base:IfIndex": FieldKind.STR,  # "eth1/1" — an interface name
    "base:Community": FieldKind.STR,  # "regular:as2-nn2:5:16" — a BGP community
    "proc:ServiceId": FieldKind.STR,
    "scalar:Date": FieldKind.STR,  # the APIC's own timestamp format, verbatim
    "scalar:Time": FieldKind.STR,
    # Class and property identifiers: these DO carry validValues — 17,654 of
    # them for mo:MoClassId alone.  They are identifiers, not a vocabulary; an
    # enum of that size would be a monument to a misreading of the schema.
    "mo:MoClassId": FieldKind.STR,
    "mo:StatsClassId": FieldKind.STR,
    "mo:StatsPropId": FieldKind.STR,
    "mo:InstanceId": FieldKind.STR,
    # Opaque fixed-width blobs (diagnostics bitmaps, index arrays).  The APIC
    # hands them over as strings and expects them back unchanged.
    "base:BitArray1024": FieldKind.STR,
    "base:IfIndexArray1024": FieldKind.STR,
    "base:Uint8Array20": FieldKind.STR,
    "base:Uint8Array40": FieldKind.STR,
    "base:Uint16Array20": FieldKind.STR,
    "base:Uint32Array20": FieldKind.STR,
    "base:Uint64Array20": FieldKind.STR,
}


def kind_for(base_type: str) -> FieldKind:
    """Return the Python kind a schema ``baseType`` maps to.

    Every family is classified in :data:`BASETYPE_MAP`; there is no fallback.
    (Earlier phases parked flags, numbers and addresses in a ``PENDING_MIGRATION``
    table that shadowed their true kind while the generators caught up — the
    migration is complete, the table is gone, and this function speaks only the
    truth.)

    Args:
        base_type: The ``baseType`` string from an APIC property schema
            (e.g. ``"scalar:Uint16"``).

    Returns:
        The :class:`FieldKind` the generators must emit.

    Raises:
        UnknownBaseTypeError: The family is not classified.  This is fatal by
            design: the previous behaviour was to guess ``str``, and that guess
            is the bug this module was written to end.

    Example:
        >>> kind_for("scalar:Bool")
        <FieldKind.BOOL: 'bool'>
        >>> kind_for("scalar:Nonesuch")
        Traceback (most recent call last):
        ...
        niwaki._codegen.basetypes.UnknownBaseTypeError: ...
    """
    try:
        return BASETYPE_MAP[base_type]
    except KeyError:
        raise UnknownBaseTypeError(
            f"unclassified schema baseType {base_type!r}. Add it to "
            "niwaki._codegen.basetypes.BASETYPE_MAP with the reason for the "
            "choice — never let it fall back to str."
        ) from None


__all__ = [
    "BASETYPE_MAP",
    "FieldKind",
    "UnknownBaseTypeError",
    "kind_for",
]
