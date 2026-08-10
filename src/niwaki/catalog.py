"""Discover and describe any Cisco ACI class — offline.

The read catalogue ships with the package: metadata for all ~15,450 ACI
classes (not just the ~2,200 with generated models), opened lazily on first
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

from niwaki.query._catalog import ClassDoc, ClassMeta, PropDoc, PropFlags
from niwaki.query._catalog import catalog as _reader

__all__ = [
    "ClassDoc",
    "ClassMeta",
    "PropDoc",
    "PropFlags",
    "class_meta",
    "concrete_subclasses",
    "describe",
    "dn_formats",
    "fault_name",
    "find_prop",
    "generated_classes",
    "prop_flags",
    "prop_meta",
    "rn_format",
    "schema_version",
    "search",
]

_generated_classes_cache: tuple[str, ...] | None = None


def generated_classes() -> tuple[str, ...]:
    """Wire names of every class the SDK generates a typed model for, sorted.

    The set behind "generated" everywhere in this module: the ~2,200 concrete,
    configurable, non-deprecated classes that ship as Pydantic models with
    readable field names. Everything else the catalogue serves dynamically.
    Offline — no APIC connection required — and derived from the code
    generator's own shipped index, so it cannot drift from the model files.

    Every returned name resolves through :func:`describe` and
    :func:`class_meta` without ``KeyError``, and every returned class is
    concrete and non-stat.

    Returns:
        Sorted, deduplicated wire class names, computed once per process.

    Example::

        classes = catalog.generated_classes()
        assert "fvBD" in classes           # configurable → has a model
        assert "topSystem" not in classes  # readable only → catalogue-served
    """
    global _generated_classes_cache
    if _generated_classes_cache is None:
        # Lazy: the package index is a plain dict (no model modules load),
        # and staying off the import path keeps `import niwaki` at budget.
        from niwaki.models._generated import _PKG_MAP

        _generated_classes_cache = tuple(sorted(_PKG_MAP))
    return _generated_classes_cache


def describe(class_name: str) -> ClassDoc:
    """Describe a class: its label, comment, properties, faults, and subclasses.

    Args:
        class_name: The wire class name, e.g. ``"fvCEp"``.

    Returns:
        A :class:`ClassDoc` — ``name``, ``label``, ``comment``, ``is_abstract``,
        ``is_observable`` (informational only — not a subscribability gate, see
        the field's own docstring), a tuple of :class:`PropDoc`, a
        ``{code: name}`` fault map, and (for an abstract class) its concrete
        subclasses.

    Raises:
        UnknownClassError: No such class in the catalogue (also a ``KeyError``).
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


def dn_formats(class_name: str) -> tuple[str, ...]:
    """Every DN shape the APIC uses for a class, as templates.

    A class is rarely reachable at a single place in the tree.  A subnet lives
    under a bridge domain, under an EPG, under a tenant, under an L2Out
    external EPG and under several service-graph nodes — a dozen shapes, one
    class.  These are those shapes, verbatim from the schema, with the
    identifying values left as ``{placeholder}``.

    **Quote them; do not rebuild them.**  A template is a fact about the
    controller, and reconstructing one by chaining parent RNs does not
    reproduce it: the containment graph is both wider than the DNs the APIC
    actually mints and, in places, missing parents that it does mint.  A
    repeated placeholder is normal and is not a mistake to correct —
    ``uni/tn-{name}/BD-{name}`` names a tenant and a bridge domain, each
    identified by ``name``.

    Args:
        class_name: The wire class name, e.g. ``"fvBD"``.

    Returns:
        The templates in schema order, duplicates included — the schema's list
        as it stands.  Empty for a class the schema gives none, which is the
        common case for an abstract class: its places belong to the concrete
        classes behind it (:func:`concrete_subclasses`).

        An **empty string is a legitimate template** — a container that prefixes
        nothing.  It can be the whole answer (six classes, the root of the tree
        among them) or sit beside real templates in the same list, so filter the
        empties out rather than testing the first element.

        The templates are not format strings: the same placeholder can name two
        different objects, so ``str.format`` on one silently builds a wrong DN.

    Raises:
        UnknownClassError: No such class in the catalogue.  Also a ``KeyError``.

    Example::

        catalog.dn_formats("fvBD")     # ('uni/tn-{name}/BD-{name}',)
        len(catalog.dn_formats("fvSubnet"))   # 12 — one class, twelve places
    """
    return _reader().dn_formats(class_name)


def prop_meta(class_name: str, name: str) -> PropDoc:
    """Describe one property of a class, addressed by its readable or wire name.

    Args:
        class_name: The wire class name, e.g. ``"fvBD"``.
        name: The property's readable (``"arp_flooding"``) or wire (``"arpFlood"``) name.

    Returns:
        A :class:`PropDoc`.

    Raises:
        UnknownClassError: The class or property is unknown (also a ``KeyError``).
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
    expose readable field names on non-generated classes. Also carries
    ``is_stat`` — whether the APIC can ever push for this class (a stats
    class, e.g. a granularity variant like ``eqptEgrBytes5min``, never can).

    Args:
        class_name: The wire class name.

    Returns:
        A :class:`ClassMeta`.

    Raises:
        UnknownClassError: No such class in the catalogue (also a ``KeyError``).
    """
    return _reader().class_meta(class_name)


def schema_version() -> str:
    """The APIC firmware the shipped catalogue and models were generated from.

    Every typed model, curated vocabulary entry and filter operator in this SDK
    derives from one firmware's schemas.  This names it, read from the shipped
    artifact itself rather than from a constant that could drift from it.

    Pair it with :attr:`niwaki.Niwaki.apic_version` — the firmware a fabric
    reports at login — to answer "am I inside the envelope this SDK was built
    for?".  Offline, like everything else in this module.

    Returns:
        The version string, e.g. ``"6.0(9c)"``.

    Example::

        assert catalog.schema_version() == "6.0(9c)"
    """
    return _reader().apic_version()


def rn_format(class_name: str) -> str:
    """The RN format of a class — the template for its own DN segment.

    A class has exactly one, regardless of where it sits: ``"BD-{name}"`` for a
    bridge domain, ``"subnet-[{ip}]"`` for a subnet.  It is the inverse key of
    DN computation — the piece a reader needs to turn a DN read back from a
    fabric into its naming values.

    Args:
        class_name: The wire class name, e.g. ``"fvBD"``.

    Returns:
        The RN format string, empty when the class defines none.

    Raises:
        UnknownClassError: No such class in the catalogue (also a ``KeyError``).

    Example::

        catalog.rn_format("fvBD")       # → "BD-{name}"
        catalog.rn_format("fvSubnet")   # → "subnet-[{ip}]"
    """
    return _reader().rn_format(class_name)


def prop_flags(class_name: str) -> dict[str, PropFlags]:
    """Every property's schema flags for a class, keyed by wire name.

    The raw material of data-driven normalisation: what is configuration
    (``is_configurable``), what the controller computes (``read_only``,
    ``implicit``), what never changes after creation (``create_only``), what
    names the object (``is_naming``), what the APIC never echoes back
    (``secure``).  One catalogue query per class, then memoised.

    Args:
        class_name: The wire class name, e.g. ``"fvBD"``.

    Returns:
        Mapping of wire property name to its :class:`PropFlags`.

    Raises:
        UnknownClassError: No such class in the catalogue (also a ``KeyError``).

    Example::

        flags = catalog.prop_flags("fvBD")
        flags["arpFlood"].is_configurable   # → True
        flags["arpFlood"].read_only         # → False
    """
    return _reader().prop_flags(class_name)
