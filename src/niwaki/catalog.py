"""Discover and describe any readable Cisco ACI class — offline.

The read catalogue ships with the package: metadata for all ~15,300 *readable*
ACI classes (not just the ~2,200 with generated models), opened lazily on first
use.  This module is the public door to it — search for a class by name or
label, describe its properties/faults/subclasses, or find which class carries a
given property — with **no APIC connection required**.

Nothing here runs at ``import niwaki``; the catalogue loads only when you import
``niwaki.catalog`` and call one of these functions.

Example::

    from niwaki import catalog

    catalog.search("bridge domain")        # → ['fvBD', ...]  (ranked)
    doc = catalog.describe("fvCEp")         # label, properties, faults, subclasses
    for prop in doc.props:
        print(prop.readable, prop.kind)     # readable field names + coercion kinds
    catalog.find_prop("mac")                # → [('fvCEp', 'mac'), ...]
    catalog.concrete_subclasses("fvEPg")    # → every concrete EPG class
    catalog.fault_name("F0467")             # → 'fltFvNwIssuesConfig-failed'
"""

from __future__ import annotations

from niwaki.query._catalog import ClassDoc, ClassMeta, PropDoc
from niwaki.query._catalog import catalog as _reader

__all__ = [
    "ClassDoc",
    "ClassMeta",
    "PropDoc",
    "class_meta",
    "concrete_subclasses",
    "describe",
    "fault_name",
    "find_prop",
    "prop_meta",
    "search",
]


def describe(class_name: str) -> ClassDoc:
    """Describe a class: its label, comment, properties, faults, and subclasses.

    Args:
        class_name: The wire class name, e.g. ``"fvCEp"``.

    Returns:
        A :class:`ClassDoc` — ``name``, ``label``, ``comment``, ``is_abstract``,
        a tuple of :class:`PropDoc`, a ``{code: name}`` fault map, and (for an
        abstract class) its concrete subclasses.

    Raises:
        KeyError: No such class in the catalogue.
    """
    return _reader().describe(class_name)


def fault_name(code: str) -> str | None:
    """The rule name behind a fault code, e.g. ``"F0467"`` → ``"fltFvNwIssuesConfig-failed"``.

    This is a *global* lookup — it does not require knowing which class raised
    the fault. That complements :func:`describe`, whose ``faults`` mapping is
    scoped to one class (the faults *that class* can raise): a
    :class:`~niwaki.models.base.ManagedObject` read back from ``faultInst``
    carries a ``code`` but not the class that raised it, so this is the
    function that turns it into a human-readable name.

    Args:
        code: The fault code, e.g. ``"F0467"``.

    Returns:
        The fault's rule name, or ``None`` if the code is not in the catalogue —
        this is expected for threshold-crossing alerts (``tca-*`` rules), whose
        codes are minted at runtime from an operator's ``statsThresholdPolicy``
        rather than defined statically in the class schema.

    Example::

        faults = aci.query("faultInst").fetch()
        for f in faults:
            print(f["code"], catalog.fault_name(f["code"]))
    """
    return _reader().fault_name(code)


def prop_meta(class_name: str, name: str) -> PropDoc:
    """Describe one property of a class, addressed by its readable or wire name.

    Args:
        class_name: The wire class name, e.g. ``"fvBD"``.
        name: The property's readable (``"arp_flooding"``) or wire (``"arpFlood"``) name.

    Returns:
        A :class:`PropDoc`.

    Raises:
        KeyError: The class or property is unknown.
    """
    return _reader().prop_meta(class_name, name)


def search(term: str, *, limit: int = 50) -> list[str]:
    """Class names whose wire name or GUI label matches ``term``.

    Ranked by the full-text index where the runtime's sqlite provides it, or a
    (broader, unranked) substring scan otherwise.

    Args:
        term: The text to match, e.g. ``"bridge domain"``.
        limit: Maximum number of class names to return.

    Returns:
        Matching wire class names.
    """
    return _reader().search(term, limit=limit)


def find_prop(term: str, *, limit: int = 50) -> list[tuple[str, str]]:
    """``(class, wire property)`` pairs whose property name or label matches ``term``.

    Answers "which class carries a MAC?" — the complement to :func:`search`.

    Args:
        term: The property text to match, e.g. ``"mac"``.
        limit: Maximum number of pairs to return.

    Returns:
        ``(wire_class_name, wire_property_name)`` pairs.
    """
    return _reader().find_prop(term, limit=limit)


def concrete_subclasses(class_name: str) -> list[str]:
    """Every concrete descendant of a class, walked transitively.

    The set an abstract-class query (e.g. ``aci.query("fvEPg")``) fans out to.

    Args:
        class_name: The (usually abstract) wire class name.

    Returns:
        Concrete descendant wire class names, sorted.
    """
    return _reader().concrete_subclasses(class_name)


def class_meta(class_name: str) -> ClassMeta:
    """A class's readable↔wire name maps and per-property coercion kinds.

    Lower-level than :func:`describe`; the same metadata the result objects use to
    expose readable field names on non-generated classes.

    Args:
        class_name: The wire class name.

    Returns:
        A :class:`ClassMeta`.

    Raises:
        KeyError: No such class in the catalogue.
    """
    return _reader().class_meta(class_name)
