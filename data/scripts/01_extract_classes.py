"""Extract all configurable concrete ACI classes from schema files.

Usage:
    uv run python data/scripts/01_extract_classes.py

Output: data/extracted/classes.json
Each entry: rn_format, identified_by, dn_formats, contained_by, contains.
contains / contained_by are filtered to configurable classes only (second pass).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "mo-apic-v6.0_9c"
OUTPUT = Path(__file__).parent.parent / "extracted" / "classes.json"


def canon(apic_ref: str) -> str:
    """Convert ACI package:Class notation to canonical name.

    Examples:
        >>> canon("fv:BD")
        'fvBD'
        >>> canon("fv:Tenant")
        'fvTenant'
    """
    return apic_ref.replace(":", "")


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


def is_config_issues_prop(prop_name: str) -> bool:
    """True for the APIC's coherence-issue channels (``configIssues`` family).

    The APIC pre-declares, per class, the catalog of "accepted but
    inconsistent" configuration states as an enum on a read-only property.
    The main property is ``configIssues``; a few classes carry variants
    (``msftConfigIssues``, ``adconfigIssues``, ``ConfigIssues``,
    ``confIssues``).  The integrity test re-states this predicate
    independently to catch drift.
    """
    lowered = prop_name.lower()
    return "configissues" in lowered or lowered == "confissues"


def extract_config_issues(properties: dict[str, Any]) -> dict[str, str]:
    """Collect the class's declared config-issue codes with descriptions.

    Scans every config-issues property (see :func:`is_config_issues_prop`)
    and returns ``{code: cleaned description}``.  The description prefers
    the entry's ``comment`` (rich prose, ~6% of values) and falls back to
    its ``label`` (human-readable phrase, 100% of values).  The
    ``defaultValue`` marker entries are skipped, everything else is kept
    verbatim (including healthy markers like ``ok`` — filtering is a
    presentation concern, not a data one).
    """
    catalog: dict[str, str] = {}
    for prop_name, prop in properties.items():
        if not isinstance(prop, dict) or not is_config_issues_prop(prop_name):
            continue
        # ``isinstance(x, dict)`` narrows to dict[Unknown, Unknown]; the cast says
        # what a schema property actually is, and keeps the walk below typed.
        values: list[dict[str, Any]] = cast("dict[str, Any]", prop).get("validValues") or []
        for entry in values:
            code = str(entry.get("localName", ""))
            if not code or code == "defaultValue":
                continue
            description = clean_comment(entry.get("comment")) or clean_comment(entry.get("label"))
            catalog.setdefault(code, description)
    return catalog


def extract_relation_info(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the relation constraints of an Rs class (empty for non-Rs).

    The schemas declare, per relation class, its ``cardinality``
    (``n-to-1``, ``1-to-1``, …) and whether the relation is ``enforceable``
    and ``resolvable`` by the APIC — the only machine-readable relation
    constraints Cisco publishes.
    """
    if not raw:
        return {}
    return {
        "cardinality": raw.get("cardinality", ""),
        "enforceable": bool(raw.get("enforceable")),
        "resolvable": bool(raw.get("resolvable")),
    }


def is_concrete_configurable(obj: dict[str, Any]) -> bool:
    """Return True for classes we want to generate: configurable, concrete, not deprecated."""
    return (
        bool(obj.get("isConfigurable"))
        and not obj.get("isAbstract")
        and not obj.get("isDeprecated")
        and not obj.get("isHidden")
    )


def extract_classes() -> dict[str, dict[str, Any]]:
    """First pass: collect all concrete configurable classes."""
    classes: dict[str, dict[str, Any]] = {}

    for schema_file in sorted(SCHEMAS_DIR.glob("*.json")):
        data: dict[str, dict[str, Any]] = json.loads(schema_file.read_text())
        class_name = next(iter(data))
        obj = data[class_name]

        if not is_concrete_configurable(obj):
            continue

        contains = [canon(k) for k in obj.get("contains", {})]
        contained_by = [canon(k) for k in obj.get("containedBy", {})]
        # An empty literal default carries no value type — name them, or the
        # walk hands partially-unknown dicts to the extractors.
        properties: dict[str, Any] = obj.get("properties") or {}
        relation_info: dict[str, Any] | None = obj.get("relationInfo")
        faults: dict[str, str] = obj.get("faults") or {}

        classes[canon(class_name)] = {
            "rn_format": obj.get("rnFormat", ""),
            "identified_by": obj.get("identifiedBy", []),
            "dn_formats": obj.get("dnFormats", []),
            "contained_by": contained_by,
            "contains": contains,
            "class_pkg": obj.get("classPkg", ""),
            "class_name": obj.get("className", ""),
            # ── Semantic metadata ───────────────────────────────────────────
            "mo_category": obj.get("moCategory", "Regular"),
            "label": obj.get("label", ""),
            "comment": clean_comment(obj.get("comment")),
            "config_issues": extract_config_issues(properties),
            "relation_info": extract_relation_info(relation_info),
            "fault_codes": dict(sorted(faults.items())),
            "write_access": sorted(obj.get("writeAccess", [])),
            # ``always`` is the only user-creatable state; ``never`` (default
            # singletons, non-creatable carriers) and ``derived`` (system-managed)
            # cannot be POST-created with a fresh name.
            "is_creatable": obj.get("isCreatableDeletable") == "always",
            "is_observable": bool(obj.get("isObservable")),
            "is_faultable": bool(obj.get("isFaultable")),
            "is_health_scorable": bool(obj.get("isHealthScorable")),
            "has_stats": bool(obj.get("hasStats")),
        }

    return classes


def main() -> None:
    if not SCHEMAS_DIR.exists():
        print(f"ERROR: schemas dir not found: {SCHEMAS_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {SCHEMAS_DIR} ...")
    classes = extract_classes()

    # Second pass: filter contains/contained_by to only known configurable classes.
    # This removes stats objects, fault delegates, health scores, etc.
    known = set(classes.keys())
    for data in classes.values():
        data["contains"] = sorted(c for c in data["contains"] if c in known)
        data["contained_by"] = sorted(c for c in data["contained_by"] if c in known)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(classes, indent=2, sort_keys=True))
    print(f"Extracted {len(classes)} classes  →  {OUTPUT}")


if __name__ == "__main__":
    main()
