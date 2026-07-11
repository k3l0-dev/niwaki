"""Utilities for deriving human-readable Python field names from ACI metadata.

ACI property names are camelCase identifiers that are meaningful inside Cisco
but opaque to Python developers.  This module turns them into snake_case names
using three data sources in priority order:

1. **JSON schema label** — the GUI display name already present in
   ``sdk_subset.json`` (e.g. ``"ARP Flooding"`` for ``arpFlood``).
2. **Scopemeta label** — extracted from APIC ishell/scopemeta binaries
   (e.g. ``"arp-flooding"``).  Used when the JSON label is missing or
   identical to the raw prop name.
3. **camelCase → snake_case** — mechanical conversion of the ACI prop name
   itself (e.g. ``arpFlood`` → ``arp_flood``).

A 40-character length cap prevents multi-word GUI labels from producing
unwieldy identifiers; those fall through to priority 3.
"""

from __future__ import annotations

import keyword
import re
from typing import Any

__all__ = [
    "MAX_LABEL_LENGTH",
    "best_field_name",
    "label_to_snake",
    "propname_to_snake",
    "resolve_py_names",
]

MAX_LABEL_LENGTH: int = 40
"""Maximum character length for a label-derived identifier.

Labels longer than this fall through to the camelCase→snake conversion.
"""

# ── Conversion helpers ────────────────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r"[\s\-/]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9_]")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")

# Two-phase camelCase split:
#   phase 1 — lowercase letter / digit followed by an uppercase letter
#             "arpFlood" → "arp_Flood"
#   phase 2 — run of uppercase letters followed by an uppercase + lowercase
#             "getHTMLParser" → "get_HTML_Parser" → "get_html_parser"
_CAMEL_LOWER_UPPER_RE = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_UPPER_RUN_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")


def label_to_snake(label: str) -> str:
    """Convert a human-readable GUI label to a snake_case Python identifier.

    Designed for title-case or hyphenated strings such as ``"ARP Flooding"``
    or ``"deployment-immediacy"``.  The conversion is deliberately simple:
    lowercase everything, replace separators with underscores, strip any
    character that is not ``[a-z0-9_]``.

    Do **not** use this for camelCase ACI prop names — use
    :func:`propname_to_snake` instead.

    Args:
        label: Human-readable display name (e.g. ``"ARP Flooding"``).

    Returns:
        snake_case identifier (e.g. ``"arp_flooding"``).  May be empty if the
        label contains no alphanumeric characters.

    Examples::

        label_to_snake("ARP Flooding")           # → "arp_flooding"
        label_to_snake("IPv6 Link Local Address") # → "ipv6_link_local_address"
        label_to_snake("deployment-immediacy")    # → "deployment_immediacy"
        label_to_snake("L3 Out")                  # → "l3_out"
    """
    s = label.lower()
    s = _SEPARATOR_RE.sub("_", s)
    s = _NON_ALNUM_RE.sub("", s)
    s = _MULTI_UNDERSCORE_RE.sub("_", s)
    return s.strip("_")


def propname_to_snake(aci_name: str) -> str:
    """Convert a camelCase ACI property name to snake_case.

    Uses a two-phase regex split to handle both regular camelCase and acronym
    runs correctly:

    - ``"arpFlood"``    → ``"arp_flood"``
    - ``"llAddr"``      → ``"ll_addr"``
    - ``"IPv6Addr"``    → ``"i_pv6_addr"``  (acronym at start — unavoidable)
    - ``"getHTMLDoc"``  → ``"get_html_doc"``

    Args:
        aci_name: ACI property name in camelCase (e.g. ``"arpFlood"``).

    Returns:
        snake_case identifier (e.g. ``"arp_flood"``).

    Examples::

        propname_to_snake("arpFlood")      # → "arp_flood"
        propname_to_snake("unicastRoute")  # → "unicast_route"
        propname_to_snake("llAddr")        # → "ll_addr"
        propname_to_snake("name")          # → "name"
    """
    s = _CAMEL_LOWER_UPPER_RE.sub(r"\1_\2", aci_name)
    s = _CAMEL_UPPER_RUN_RE.sub(r"\1_\2", s)
    return s.lower()


# ── Main public function ──────────────────────────────────────────────────────


def best_field_name(
    aci_name: str,
    json_label: str = "",
    sm_label: str = "",
    *,
    is_naming: bool = False,
) -> str:
    """Return the best Python field name for an ACI property.

    Tries three sources in priority order and returns the first usable result:

    1. **JSON schema label** (``json_label``) — used when it is meaningfully
       different from ``aci_name`` (case-insensitive comparison) **and** the
       resulting snake_case identifier is at most :data:`MAX_LABEL_LENGTH`
       characters long and is a valid Python identifier.
    2. **Scopemeta label** (``sm_label``) — used as a fallback when the JSON
       label was informative but its candidate was rejected (too long or
       starts with a digit).  Skipped entirely for *naming* props because
       APIC's scopemeta ``_propLabel`` for identifying properties (``name``,
       ``dn``) often stores the class display name rather than a property
       description.
    3. **camelCase→snake conversion** of ``aci_name`` itself.

    Python keywords (``from``, ``class``, …) are suffixed with ``_`` as the
    very first step, before any label-based renaming.

    Args:
        aci_name:   Raw ACI property name, e.g. ``"arpFlood"``.
        json_label: GUI display label from the JSON schema, e.g.
                    ``"ARP Flooding"``.  Pass ``""`` when unavailable.
        sm_label:   Label extracted from APIC scopemeta binaries, e.g.
                    ``"arp-flooding"``.  Pass ``""`` when unavailable.
        is_naming:  ``True`` when the property is a naming (identifying) prop.
                    Disables the scopemeta fallback for naming props to avoid
                    misleading class-description labels.

    Returns:
        A valid, non-empty Python identifier in snake_case.

    Examples::

        best_field_name("arpFlood", "ARP Flooding", "")
        # → "arp_flooding"   (priority 1: JSON label)

        best_field_name("resImedcy", "Resolution Immediacy", "resolution-immediacy")
        # → "resolution_immediacy"  (priority 1: JSON label)

        best_field_name("floodOnEncap",
                        "Handling of L2 Multicast/Broadcast and Link Layer Traffic",
                        "flood-on-encap")
        # → "flood_on_encap"  (priority 2: scopemeta label, JSON label too long)

        best_field_name("name", "Name", "enable-infrastructure-vlan", is_naming=True)
        # → "name"  (scopemeta skipped for naming props)

        best_field_name("from", "From", "")
        # → "from_"  (Python keyword guard)
    """
    # Python keyword guard — always applied first.
    if keyword.iskeyword(aci_name):
        return f"{aci_name}_"

    aci_lower = aci_name.lower()
    json_informative = bool(json_label) and json_label.lower() != aci_lower

    # Priority 1: JSON schema label
    if json_informative:
        candidate = label_to_snake(json_label)
        if candidate and len(candidate) <= MAX_LABEL_LENGTH and candidate.isidentifier():
            return candidate

    # Priority 2: Scopemeta label.
    # For non-naming props: consulted both when JSON label was informative-but-
    # rejected AND when JSON label equalled aci_name (scopemeta may have a better
    # human-readable form, e.g. "purgeWin" → "purge-window-size").
    # For naming props: skipped entirely.  APIC's scopemeta _propLabel for
    # identifying properties (name, dn, …) often stores the class display name
    # rather than a property description, producing misleading renames like
    # infraAttEntityP.name → "enable-infrastructure-vlan".
    if not is_naming and sm_label and sm_label.lower() != aci_lower:
        candidate = label_to_snake(sm_label)
        if candidate and len(candidate) <= MAX_LABEL_LENGTH and candidate.isidentifier():
            return candidate

    # Priority 3: camelCase → snake_case conversion
    return propname_to_snake(aci_name)


# Curated field-name overrides: (ACI class, wire prop) → Python name.
# For the rare props whose GUI label produces an unusable identifier while
# the wire name itself is the word operators use.  Applied before any
# label-derived naming; keep this list short and justified.
FIELD_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    # Label "Visibility of the subnet" → the operator word is just "scope"
    # (private / public / shared).
    ("fvSubnet", "scope"): "scope",
}


def resolve_py_names(
    props: dict[str, Any],
    sm_class: dict[str, str],
    aci_class: str = "",
) -> dict[str, str]:
    """Return ``{aci_prop: python_name}`` for every prop in a class.

    Computes the best Python name for each prop via :func:`best_field_name`,
    then detects intra-class collisions (two props mapping to the same name).
    When a collision occurs, the naming prop keeps the label-derived name; the
    non-naming prop(s) fall back to :func:`propname_to_snake` (priority-3).
    If two non-naming props collide, the alphabetically-first one wins.

    Args:
        props:    ``{aci_prop_name: prop_dict}`` from ``sdk_subset.json``.
                  Each prop_dict should have at least a ``"python_type"`` key
                  and optional ``"label"``, ``"is_naming"`` keys.
        sm_class: ``{aci_prop_name: sm_label}`` from scopemeta.
        aci_class: ACI class name, used to look up
            :data:`FIELD_NAME_OVERRIDES` (empty string disables overrides).

    Returns:
        ``{aci_prop_name: resolved_py_name}`` for every prop in the class.

    Examples::

        resolve_py_names(
            {"featureName": {"label": "Entitlement TAG Name", "is_naming": True, ...},
             "mode":        {"label": "Entitlement TAG Name", "is_naming": False, ...}},
            {},
        )
        # → {"featureName": "entitlement_tag_name", "mode": "mode"}
        # (naming prop keeps the label; non-naming falls back to camelCase→snake)
    """
    name_to_aci: dict[str, list[str]] = {}
    for pn, pd in props.items():
        if (override := FIELD_NAME_OVERRIDES.get((aci_class, pn))) is not None:
            pyn = override
        else:
            pyn = best_field_name(
                pn, pd.get("label", ""), sm_class.get(pn, ""), is_naming=bool(pd.get("is_naming"))
            )
        name_to_aci.setdefault(pyn, []).append(pn)

    result: dict[str, str] = {}
    for pyn, aci_list in name_to_aci.items():
        if len(aci_list) == 1:
            result[aci_list[0]] = pyn
        else:
            # Winner: naming prop first; ties broken alphabetically.
            sorted_aci = sorted(aci_list, key=lambda a: (not bool(props[a].get("is_naming")), a))
            result[sorted_aci[0]] = pyn
            for aci_name in sorted_aci[1:]:
                # Force priority-3 (keyword-safe camelCase→snake) for losers.
                result[aci_name] = best_field_name(aci_name, "", "")
    return result
