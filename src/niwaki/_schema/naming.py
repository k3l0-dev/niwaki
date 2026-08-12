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

A label is only usable as a *name* when it is one — an acceptance gate
rejects label candidates that read as *descriptions* (too many words, or
containing grammar words such as "of"/"whether"), on top of the historical
40-character cap.  Rejected candidates fall through to the next source; the
wire-name conversion (priority 3) is never filtered, so legitimate jargon
that happens to contain a gated word survives (``flood_on_encap``,
``is_default``, ``mcp_pdu_per_vlan``).

Cisco ships a handful of misspelled labels; :data:`LABEL_CORRECTIONS` fixes
those typos at the label-token level, inside the common funnel
(:func:`label_to_snake`), so every consumer — field names, navigation names,
docs — sees the corrected spelling.

The module also hosts :data:`NAV_NAME_OVERRIDES` — curated fixes for facade
*navigation* names (class → name), a distinct axis from the per-property
field names above; ``generate_domain`` is its consumer.
"""

from __future__ import annotations

import keyword
import re
from typing import Any

__all__ = [
    "FIELD_NAME_OVERRIDES",
    "LABEL_CORRECTIONS",
    "LABEL_MARKERS",
    "MAX_LABEL_LENGTH",
    "MAX_LABEL_WORDS",
    "NAV_NAME_OVERRIDES",
    "best_field_name",
    "classname_to_snake",
    "label_to_snake",
    "propname_to_snake",
    "resolve_py_names",
]

MAX_LABEL_LENGTH: int = 40
"""Maximum character length for a label-derived identifier.

Labels longer than this fall through to the camelCase→snake conversion.
"""

MAX_LABEL_WORDS: int = 4
"""Maximum word count for a label-derived identifier.

A label with five or more words is a sentence describing the property, not
a name for it (``"Indicate whether MPLS is enabled or not"``).  Candidates
above this cap fall through to the next source, which for Cisco schemas is
almost always the wire name — the spelling operators actually know.
"""

LABEL_MARKERS: frozenset[str] = frozenset(
    {
        # Pure grammar words — a label containing one is prose, not a name.
        "a",
        "an",
        "and",
        "are",
        "be",
        "by",
        "can",
        "for",
        "has",
        "have",
        "in",
        "indicate",
        "indicates",
        "into",
        "is",
        "not",
        "of",
        "on",
        "or",
        "per",
        "specifies",
        "specify",
        "that",
        "the",
        "this",
        "was",
        "when",
        "where",
        "whether",
        "which",
        "with",
    }
)
"""Grammar words that mark a label as a description rather than a name.

A label-derived candidate containing any of these tokens is rejected and
falls through to the next source.  The wire-name fallback is never gated,
so jargon keeps its prepositions (``flood_on_encap``, ``to_port_id``).

Deliberate, measured exclusions — these read like grammar words but are
load-bearing network jargon in real labels, and gating them degrades names:

- ``from``/``to`` — ``destination_from_port`` (vzEntry),
  ``from_node_id``/``to_node_id`` (infraNodeBlk), ``time_to_live``;
- ``as`` — ``private_as_control`` (BGP autonomous system; the wire fallback
  ``privateASctrl`` camel-splits into ``private_a_sctrl``), ``as_path_criteria``;
- ``use``/``used``/``using`` — ``use_configured_system_gipo`` (the wire
  fallback mangles the GIPo acronym into ``gi_po``).
"""

LABEL_CORRECTIONS: dict[str, str] = {
    # Cisco ships "Catalog Maitenance Policy" (maintCatMaintP) — fix the typo.
    "maitenance": "maintenance",
    # Cisco ships "VMM Host Availibility Policy" (vmmHvAvailPol) and
    # "Enable Host availibility monitoring" (vmmDomP.hvAvailMonitor).
    "availibility": "availability",
}
"""Token-level spelling fixes for typos Cisco ships in schema labels.

Applied inside :func:`label_to_snake` — the common funnel — so the fix
reaches every derived surface (model fields, navigation, docs) without
per-class overrides.  Keys and values are single lowercase snake tokens.

The regen pipeline verifies that every key still occurs in the schema
corpus and fails the build otherwise: a correction whose typo Cisco has
since fixed is dead curation and must be deleted, loudly.
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

    Cisco label typos listed in :data:`LABEL_CORRECTIONS` are fixed here,
    token by token, so every consumer of this funnel (field names,
    navigation names, docs) sees the corrected spelling.

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
        label_to_snake("Catalog Maitenance Policy")
        # → "catalog_maintenance_policy"  (typo corrected)
    """
    s = _label_to_snake_raw(label)
    if s:
        s = "_".join(LABEL_CORRECTIONS.get(token, token) for token in s.split("_"))
    return s


def _label_to_snake_raw(label: str) -> str:
    """The :func:`label_to_snake` conversion *without* typo corrections.

    Exists so the regen guard can decide whether a LABEL_CORRECTIONS entry is
    alive using the funnel's own tokenization: a correction key is alive iff
    it appears as a token of some label's raw snake form — exactly the tokens
    the corrected funnel would rewrite.
    """
    s = label.lower()
    s = _SEPARATOR_RE.sub("_", s)
    s = _NON_ALNUM_RE.sub("", s)
    s = _MULTI_UNDERSCORE_RE.sub("_", s)
    return s.strip("_")


def _accept_label_candidate(candidate: str) -> bool:
    """Return ``True`` when a label-derived candidate is usable as a name.

    The gate applies to *label* candidates only (JSON schema label and
    scopemeta label) — the wire-name conversion is never filtered.  A
    candidate passes when it is a valid Python identifier that is not a
    Python keyword (Cisco labels a prop "Class" → ``class`` would be
    unreachable as an attribute), at most :data:`MAX_LABEL_LENGTH`
    characters, at most :data:`MAX_LABEL_WORDS` words, and contains no
    :data:`LABEL_MARKERS` token.
    """
    if not candidate or not candidate.isidentifier() or keyword.iskeyword(candidate):
        return False
    if len(candidate) > MAX_LABEL_LENGTH:
        return False
    tokens = candidate.split("_")
    if len(tokens) > MAX_LABEL_WORDS:
        return False
    return not any(token in LABEL_MARKERS for token in tokens)


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


def classname_to_snake(class_name: str) -> str:
    """Convert a PascalCase ACI ``className`` to snake_case.

    Splits **only** at lowercase/digit → uppercase boundaries, which is the
    correct rule for PascalCase class names where leading acronym runs are
    single tokens — unlike :func:`propname_to_snake`, whose second phase
    splits acronym runs and mangles class names (``"EPg"`` → ``"e_pg"``,
    ``"ThrValueUByte"`` → ``"thr_value_u_byte"``).

    Args:
        class_name: ACI ``className`` in PascalCase (e.g. ``"DevFolder"``).

    Returns:
        snake_case identifier (e.g. ``"dev_folder"``).

    Examples::

        classname_to_snake("DevFolder")      # → "dev_folder"
        classname_to_snake("EPg")            # → "epg"
        classname_to_snake("ThrValueUByte")  # → "thr_value_ubyte"
        classname_to_snake("RsSrcToVPortDef")  # → "rs_src_to_vport_def"
    """
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", class_name).lower()


# Curated navigation-name overrides: ACI class → facade navigation name.
# Wins over every derivation source (curated maker names excepted — the
# vocabulary overlay is applied after derivation and is authoritative at
# curated positions).  The mechanism is kept as the escape hatch for a name
# wrong at the source in a way no data fix can express; the historical typo
# entries moved to LABEL_CORRECTIONS (fixed in the funnel, all surfaces).
NAV_NAME_OVERRIDES: dict[str, str] = {}


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
       different from ``aci_name`` (case-insensitive comparison) **and** it
       passes the acceptance gate: valid identifier, at most
       :data:`MAX_LABEL_LENGTH` characters, at most :data:`MAX_LABEL_WORDS`
       words, no :data:`LABEL_MARKERS` token (a label that reads as a
       sentence is a description, not a name).
    2. **Scopemeta label** (``sm_label``) — same acceptance gate; used as a
       fallback when the JSON label was rejected.  Skipped entirely for
       *naming* props because APIC's scopemeta ``_propLabel`` for
       identifying properties (``name``, ``dn``) often stores the class
       display name rather than a property description.
    3. **camelCase→snake conversion** of ``aci_name`` itself — never gated:
       the wire name is the truth of the fabric, however verbose.

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
        # → "flood_on_encap"  (both labels gated — "of"/"on" are markers —
        #    and the wire name spells the same jargon; priority 3)

        best_field_name("mplsEnabled", "Indicate whether MPLS is enabled or not", "")
        # → "mpls_enabled"  (sentence label gated → wire name)

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
        if _accept_label_candidate(candidate):
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
        if _accept_label_candidate(candidate):
            return candidate

    # Priority 3: camelCase → snake_case conversion
    return propname_to_snake(aci_name)


# Curated field-name overrides: (ACI class, wire prop) → Python name.
# The true irreducibles: positions where the acceptance gate cannot produce
# the right name and no data fix can express it.  Each entry is a judgment
# call with its justification; the table targets ~0 and every removal must
# come from the derivation itself (never grow this back into a patch list —
# 82 wire-spelling pins dissolved into the gate in 2.0).
FIELD_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    # Label "Preferred as primary subnet" is 4 words with no LABEL_MARKERS
    # token ("as" is deliberately excluded from the markers to protect names
    # like bgpPeerP.private_as_control); the operator word is the wire prop.
    ("fvSubnet", "preferred"): "preferred",
    # Scopemeta says "allow-fragments" but the sibling class vzEntryPortZero
    # has no scopemeta entry and derives apply_to_frag from the wire name.
    # Keep the sibling classes consistent and aligned with the GUI term
    # ("Apply To Fragment").
    ("vzEntry", "applyToFrag"): "apply_to_frag",
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
            # Winner: an overridden prop first (curation is explicit — it must
            # never silently lose its pinned name to a derived sibling); then a
            # naming prop; then the prop that *owns* the name — the one whose
            # own camelCase→snake spelling already is it.  Cisco gives
            # ospfExtP.areaType and ospfExtP.areaCtrl the same label ("Area
            # Type"), and handing the name to areaCtrl leaves areaType with a
            # fallback that is the very same name: the two collide again and one
            # property vanishes from the SDK entirely.  Letting the natural owner
            # keep it means every loser falls back to a spelling of its own.
            sorted_aci = sorted(
                aci_list,
                key=lambda a: (
                    (aci_class, a) not in FIELD_NAME_OVERRIDES,
                    not bool(props[a].get("is_naming")),
                    best_field_name(a, "", "") != pyn,
                    a,
                ),
            )
            result[sorted_aci[0]] = pyn
            for aci_name in sorted_aci[1:]:
                # Force priority-3 (keyword-safe camelCase→snake) for losers.
                result[aci_name] = best_field_name(aci_name, "", "")

    # A curated override that did not survive resolution is curation silently
    # dropped — that must be a build error, never a quiet reassignment.
    for pn in props:
        if (pinned := FIELD_NAME_OVERRIDES.get((aci_class, pn))) is not None and result[
            pn
        ] != pinned:
            raise ValueError(
                f"{aci_class}.{pn}: FIELD_NAME_OVERRIDES pins {pinned!r} but "
                f"resolution produced {result[pn]!r} — the override collided "
                "and lost. Fix the colliding sibling or the override."
            )

    # A property that shares its Python name with another is a property the SDK
    # silently drops — the model keeps whichever the template wrote last.  It has
    # happened; it must never happen quietly again.
    if len(set(result.values())) != len(result):
        duplicates = sorted(
            name for name in set(result.values()) if list(result.values()).count(name) > 1
        )
        raise ValueError(
            f"{aci_class or '<class>'}: Python name collision on {duplicates} — "
            "two ACI properties would map to the same field and one would be "
            "dropped. Add a FIELD_NAME_OVERRIDES entry."
        )
    return result
