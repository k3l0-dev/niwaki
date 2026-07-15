"""Extract and normalize configurable properties for each known ACI class.

Reads classes from data/extracted/classes.json (output of 01_extract_classes.py).

Usage:
    uv run python data/scripts/02_extract_props.py

Output: data/extracted/properties.json

Each property entry is normalized to a codegen-ready dict:
    python_type   : str | bool | literal | int
    default       : Python-typed default value (None = required / no default)
    is_naming     : bool — forms the RN
    create_only   : bool — write-once (only settable on object creation)
    mandatory     : bool — APIC requires this field
    label         : human-readable name
    min_length    : int  (str only)
    max_length    : int  (str only)
    pattern       : str  (str only)
    validate_as   : "ip" | "mac"  (str only)
    values        : list[str]  (literal only)
    ge / le       : int  (int only)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from niwaki._codegen.basetypes import FieldKind, kind_for

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "mo-apic-v6.0_9c"
CLASSES_FILE = Path(__file__).parent.parent / "extracted" / "classes.json"
OUTPUT = Path(__file__).parent.parent / "extracted" / "properties.json"

# The only family whose schema declares string lengths and a regex.  Every other
# string is opaque to us (a DN, an encap, a password) and carries no constraint.
_LENGTH_CONSTRAINED = frozenset({"string:Basic"})


def _is_float(value: str) -> bool:
    """True when *value* parses as a real number."""
    try:
        float(value)
    except ValueError:
        return False
    return True


# ── Enum / literal helpers ────────────────────────────────────────────────────


def clean_comment(raw: object, limit: int = 300) -> str:
    """Normalize a schema ``comment`` into a single documentation line.

    Schemas store comments as a list of strings; some run to ~1000 chars.
    Join, collapse whitespace, and truncate at a sentence boundary (or hard
    at *limit* with an ellipsis) so the text fits IDE hovers and docstrings.

    Args:
        raw: The raw ``comment`` value (list of strings, string, or None).
        limit: Maximum length of the returned text.

    Returns:
        A cleaned one-line description, or ``""`` when there is none.
    """
    if not raw:
        return ""
    # A schema comment is a string or a list of lines.  ``isinstance`` narrows a
    # list to Unknown elements, so the cast says what the schema actually holds.
    parts: list[object] = cast("list[object]", raw) if isinstance(raw, list) else [raw]
    text = " ".join(str(part) for part in parts)
    text = " ".join(text.split())
    # Cisco placeholder comments ("null", "TBD"…) document nothing — drop them.
    if text.lower().rstrip(".") in {"null", "none", "na", "n/a", "tbd", "todo"}:
        return ""
    if len(text) > limit:
        cut = text[:limit]
        dot = cut.rfind(". ")
        text = cut[: dot + 1] if dot > 80 else cut.rstrip() + "…"
    return text


def _is_numeric(val: str) -> bool:
    """Return True if *val* is a decimal or hex integer string.

    Handles ``"0"``, ``"42"``, ``"0x806"``, ``"0xABCD"`` — all numeric aliases
    used in ACI schemas for enum entries whose human-readable name lives in
    ``localName``.
    """
    try:
        int(val, 0)
        return True
    except ValueError:
        return False


def _extract_enum_values(
    valid_values: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str], dict[str, str], dict[str, int]]:
    """Return ``(values, aliases, value_comments, weights)`` for an ACI enum.

    ACI schemas encode enum entries in two patterns:

    - Numeric ``value`` + human ``localName``  →  ``{'value': '0x806', 'localName': 'arp'}``
    - String default marker                    →  ``{'value': 'arp', 'localName': 'defaultValue'}``

    We collect the human-readable ``localName`` for every non-marker entry and
    build an ``aliases`` dict mapping each numeric ``value`` back to its
    ``localName`` (e.g. ``{"0x806": "arp"}``) for use in ``StrEnum._missing_``.
    When an entry carries a Cisco ``comment``, it is kept in ``value_comments``.

    The numeric ``value`` is also kept as a **weight**.  For an enum it is
    incidental; for a bitmask it is the bit position, and it is the order the
    APIC itself serialises in — ``lacpLagPol.ctrl`` defaults to
    ``"susp-individual,graceful-conv,fast-sel-hot-stdby"``, i.e. weights 1, 2, 8.
    Sorting the members alphabetically, as this function used to, threw that
    away, and no amount of care downstream could reconstruct it.

    Args:
        valid_values: Raw ``validValues`` list from an ACI JSON schema property.

    Returns:
        A tuple of:
        - Sorted list of canonical ``localName`` strings.
        - Dict mapping numeric string aliases to their canonical ``localName``.
        - Dict mapping canonical values to their cleaned Cisco descriptions.
        - Dict mapping canonical values to their numeric weight (bit position).
    """
    values: set[str] = set()
    aliases: dict[str, str] = {}
    value_comments: dict[str, str] = {}
    weights: dict[str, int] = {}

    for entry in valid_values:
        val: str = entry.get("value", "")
        local_name: str = entry.get("localName", "")

        canonical = ""
        if _is_numeric(val):
            if local_name and local_name != "defaultValue":
                values.add(local_name)
                aliases[val] = local_name
                weights[local_name] = int(val, 0)
                canonical = local_name
        elif local_name != "defaultValue":
            values.add(val)
            canonical = val

        if canonical and (comment := clean_comment(entry.get("comment"))):
            value_comments[canonical] = comment

    return sorted(values), aliases, value_comments, weights


# ── Constraints ───────────────────────────────────────────────────────────────


def _numeric_range(validators: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """The bounds a numeric property accepts, across **all** its validators.

    A schema may declare several ranges: ``bgpCtxPol.holdIntvl`` carries
    ``[{min: 0, max: 0}, {min: 3, max: 3600}]`` — the legal set is ``{0}`` union
    ``[3, 3600]``, and its own default is 180.  Reading only the first validator
    (which is what this extractor used to do) yields ``le = 0`` and produces a
    model that rejects its own default.

    The union is taken instead: the gap in the middle is the APIC's business,
    and the APIC is the one that arbitrates it.

    A ``max`` of **0 means "no upper bound"** — it is Cisco's sentinel, not a
    ceiling.  ``bgpAsP.asn`` declares ``{min: 1, max: 0}``: read literally that
    is an empty range, and a model built from it would reject every AS number
    ever written.  So a zero maximum is dropped, not honoured.

    Args:
        validators: The ``validators`` list from a property schema.

    Returns:
        ``(ge, le)`` — the lowest minimum and the highest *declared* maximum, or
        ``None`` for either bound the schema leaves open.
    """
    mins = [v["min"] for v in validators if isinstance(v.get("min"), int)]
    maxes = [v["max"] for v in validators if isinstance(v.get("max"), int) and v["max"] > 0]
    return (min(mins) if mins else None, max(maxes) if maxes else None)


def _string_constraints(
    validators: list[dict[str, Any]],
) -> tuple[int, int | None, str | None]:
    """Length bounds and pattern of a string property, across all validators.

    The regex does not always live in the first validator (``vmmInjectedSvcEp
    .name`` keeps it in the second), and a pattern silently dropped is a
    validation the SDK claims to do and does not.

    Args:
        validators: The ``validators`` list from a property schema.

    Returns:
        ``(min_length, max_length, pattern)``.
    """
    min_length = min((v["min"] for v in validators if isinstance(v.get("min"), int)), default=0)
    max_length = max((v["max"] for v in validators if isinstance(v.get("max"), int)), default=None)
    pattern = next(
        (regexs[0]["regex"] for v in validators if (regexs := v.get("regexs"))),
        None,
    )
    return min_length, max_length, pattern


def _numeric_default(
    raw: object, kind: FieldKind, names: list[str], aliases: dict[str, str]
) -> object:
    """The default of a number — which the APIC may express as a name.

    ``bgpCtxPol.staleIntvl`` is a ``Uint16`` whose declared default is the string
    ``"default"`` (an alias for 300).  Coercing that to ``0``, as a
    number-or-nothing rule does, would make the model disagree with the fabric
    from the very first read.

    Args:
        raw: The schema's ``default``, verbatim.
        kind: ``INT`` or ``FLOAT``.
        names: The named values the property accepts.
        aliases: Number → name, for the values the APIC renames.

    Returns:
        The default as an ``int``/``float``, or as the *name* the APIC stores it
        under when it has one.
    """
    text = str(raw) if raw is not None else ""
    if not text:
        return 0 if kind is FieldKind.INT else 0.0
    if text in names:
        return text
    # A number the APIC would rename: keep the name, since that is what it stores.
    if text in aliases:
        return aliases[text]
    if kind is FieldKind.INT and text.lstrip("-").isdigit():
        return int(text)
    if kind is FieldKind.FLOAT and _is_float(text):
        return float(text)
    return 0 if kind is FieldKind.INT else 0.0


def _flags_default(raw: str, values: list[str], aliases: dict[str, str]) -> list[str]:
    """The default of a bitmask — a **set** of members, not one of them.

    A bitmask default is comma-joined (``lacpLagPol.ctrl`` defaults to
    ``"susp-individual,graceful-conv,fast-sel-hot-stdby"``), so the rule used for
    single-choice enums — "the declared default if it is a member, else the
    first member" — cannot see it and falls back to an arbitrary member.  That is
    how ``bgpBestPathCtrlPol.ctrl``, whose schema default is ``"0"`` (i.e. *no
    flags at all*), came to ship with ``asPathMultipathRelax`` enabled: the SDK
    was turning a BGP behaviour on that nobody asked for.

    Args:
        raw: The schema's ``default``, verbatim.
        values: The member names.
        aliases: Numeric value → member name (``"2"`` → ``"private"``).

    Returns:
        The default members, in the order the schema declared them.  An empty
        list when the default is the zero-mask (``"0"``) or absent — an empty set
        of flags is a perfectly ordinary default.
    """
    members: list[str] = []
    for part in (piece.strip() for piece in str(raw or "").split(",")):
        if not part:
            continue
        member = part if part in values else aliases.get(part, "")
        # "0" is the zero-mask: no flags.  It resolves to no member, and that is
        # the correct answer, not a miss.
        if member and member not in members:
            members.append(member)
    return members


# ── Per-prop normalizer ───────────────────────────────────────────────────────


def normalize_prop(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw ACI property schema to a codegen-ready dict.

    The ``baseType`` decides everything, and it is looked up in
    :data:`niwaki._codegen.basetypes.BASETYPE_MAP` — a family this SDK has never
    classified **raises** rather than quietly becoming a string.  That silent
    fallback is what typed half the SDK's numbers as text.

    Args:
        schema: Raw property dict from an ACI JSON schema.

    Returns:
        The normalized property, or ``None`` when the schema declares no
        ``baseType`` at all (nothing to map).

    Raises:
        UnknownBaseTypeError: The property's ``baseType`` is not classified.
    """
    base_type: str = schema.get("baseType", "")
    if not base_type:
        return None

    kind = kind_for(base_type)
    validators: list[dict[str, Any]] = schema.get("validators", [])

    result: dict[str, Any] = {
        "is_naming": bool(schema.get("isNaming")),
        "create_only": bool(schema.get("createOnly")),
        "mandatory": bool(schema.get("mandatory")),
        "secure": bool(schema.get("secure")),
        "label": (schema.get("label") or "").strip(),
        "comment": clean_comment(schema.get("comment")),
        # The schema's own default, verbatim.  Kept for every kind because the
        # rendered default is derived from it — and because a flags default is a
        # comma-joined set ("susp-individual,graceful-conv,fast-sel-hot-stdby")
        # that no single-value rule could ever reconstruct.
        "schema_default": schema.get("default"),
    }

    if kind is FieldKind.BOOL:
        result["python_type"] = "bool"
        result["default"] = schema.get("default", "false") in ("true", "yes")
        return result

    if kind in (FieldKind.ENUM, FieldKind.FLAGS):
        values, aliases, value_comments, weights = _extract_enum_values(
            schema.get("validValues", [])
        )
        if values:
            if kind is FieldKind.FLAGS:
                # Declare the members in ascending bit weight — the order the
                # APIC serialises them in.  Every generator downstream then gets
                # the canonical wire order for free, from the enum's own
                # declaration, with nothing to look up and nothing to agree on.
                values = sorted(values, key=lambda member: weights.get(member, 0))
            result["python_type"] = kind.value
            result["values"] = values
            result["aliases"] = aliases
            result["value_comments"] = value_comments
            result["weights"] = weights
            result["model_type"] = schema.get("modelType", "")
            raw_default = schema.get("default", "")
            if kind is FieldKind.FLAGS:
                result["default"] = _flags_default(raw_default, values, aliases)
            else:
                result["default"] = raw_default if raw_default in values else values[0]
        else:
            # An enum the schema declares without a single value: nothing to
            # generate, and a Literal of nothing is not a type.
            result["python_type"] = "str"
            result["default"] = schema.get("default", "")
        return result

    if kind in (FieldKind.INT, FieldKind.FLOAT):
        result["python_type"] = kind.value
        ge, le = _numeric_range(validators)
        if ge is not None:
            result["ge"] = ge
        if le is not None:
            result["le"] = le

        # A number the APIC may *name*.  592 numeric properties declare named
        # values — a filter port of 80 is stored as "http", a BGP stale interval
        # of 300 as "default", an unset port as "unspecified" — and the APIC
        # canonicalises to the name on write.  Reading only the number would
        # leave the model comparing 80 against "http" for ever.
        names, aliases, value_comments, _ = _extract_enum_values(schema.get("validValues", []))
        if names:
            result["values"] = names
            result["aliases"] = aliases
            result["value_comments"] = value_comments

        raw = schema.get("default")
        result["default"] = _numeric_default(raw, kind, names, aliases)
        return result

    if kind in (FieldKind.IP, FieldKind.MAC):
        result["python_type"] = "str"
        result["validate_as"] = "ip" if kind is FieldKind.IP else "mac"
        result["default"] = ""
        return result

    # ── str, by decision ──────────────────────────────────────────────────────
    result["python_type"] = "str"
    if base_type in _LENGTH_CONSTRAINED:
        min_length, max_length, pattern = _string_constraints(validators)
        result["min_length"] = min_length
        if max_length is not None:
            result["max_length"] = max_length
        if pattern is not None:
            result["pattern"] = pattern
        # A naming prop has no default: the caller must name the object.
        result["default"] = None if schema.get("isNaming") else ""
    else:
        result["default"] = ""
    return result


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    if not CLASSES_FILE.exists():
        print("ERROR: run 01_extract_classes.py first", file=sys.stderr)
        sys.exit(1)

    classes: dict[str, dict[str, Any]] = json.loads(CLASSES_FILE.read_text())
    print(f"Processing {len(classes)} classes ...")

    properties: dict[str, dict[str, Any]] = {}

    for class_name in sorted(classes):
        schema_file = SCHEMAS_DIR / f"{class_name}.json"
        if not schema_file.exists():
            continue

        data = json.loads(schema_file.read_text())
        # The JSON key uses the fv:BD notation, not the canonical fvBD name
        actual_key = next(iter(data))
        props_schema: dict[str, Any] = data[actual_key].get("properties", {})

        class_props: dict[str, dict[str, Any]] = {}
        for prop_name, prop_schema in props_schema.items():
            if (
                not prop_schema.get("isConfigurable")
                or prop_schema.get("isDeprecated")
                or prop_schema.get("implicit")
            ):
                continue

            normalized = normalize_prop(prop_schema)
            if normalized is not None:
                class_props[prop_name] = normalized

        if class_props:
            properties[class_name] = class_props

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(properties, indent=2, sort_keys=True))
    print(f"Extracted properties for {len(properties)} classes  →  {OUTPUT}")


if __name__ == "__main__":
    main()
