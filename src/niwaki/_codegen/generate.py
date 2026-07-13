"""Codegen entry point: read sdk_subset.json and emit one .py per ACI class.

Usage (from repo root):
    uv run python -m niwaki._codegen.generate

Input:  data/extracted/sdk_subset.json
Output: src/niwaki/models/_generated/<ClassName>.py
        src/niwaki/models/_generated/__init__.py  (pkgutil auto-discover)
        tests/models/_test_data.json              (parametrised test fixtures)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from niwaki._codegen._label_utils import best_field_name, propname_to_snake, resolve_py_names

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent.parent.parent  # …/niwaki/
SUBSET_FILE = _REPO_ROOT / "data" / "extracted" / "sdk_subset.json"
MAPPING_FILE = _REPO_ROOT / "data" / "extracted" / "enum_mapping.json"
SM_LABELS_FILE = _REPO_ROOT / "data" / "extracted" / "scopemeta_labels.json"
TEMPLATES_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = _REPO_ROOT / "src" / "niwaki" / "models" / "_generated"
TEST_DATA_FILE = _REPO_ROOT / "tests" / "models" / "_test_data.json"

# ── Codegen inputs ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CodegenInputs:
    """Everything the renderers need beyond ``sdk_subset.json``.

    Loaded once in :func:`main` and passed explicitly — no module-level
    mutable state.

    Attributes:
        enum_mapping: ``"model_type||val1|val2|…"`` → StrEnum class name
            (produced by ``generate_enums.py``).
        sm_labels: Dotted class name (``"fv.BD"``) → ``{prop: label}``
            scopemeta display labels.
        child_map: Parent ACI class → ``{jargon name → child ACI class}``
            (from ``domain._child_map``; empty when not yet generated).
        rs_target_prop: Rs singleton ACI class → its ``tn*Name`` prop.
        class_pkg: ACI class → package directory name.
    """

    enum_mapping: dict[str, str] = field(default_factory=dict)
    sm_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    child_map: dict[str, dict[str, str]] = field(default_factory=dict)
    rs_target_prop: dict[str, str] = field(default_factory=dict)
    class_pkg: dict[str, str] = field(default_factory=dict)


# ── Validation patterns ───────────────────────────────────────────────────────

_IP_PATTERN = r"^[0-9a-fA-F.:/ ]+$"
_MAC_PATTERN = r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$"

# ── Valid test value used when building test fixtures ────────────────────────
# "ab" passes all 9 ACI naming-prop patterns (incl. hex-only ^[a-fA-F0-9]+$),
# satisfies min_length=1, and fits within max_length=64.
_VALID_VAL = "ab"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _enum_member(local_name: str) -> str:
    """Convert an enum localName to UPPER_SNAKE_CASE Python identifier.

    Args:
        local_name: ACI enum localName (e.g. ``"bd-flood"``).

    Returns:
        UPPER_SNAKE_CASE identifier (e.g. ``"BD_FLOOD"``).
    """
    name = re.sub(r"[^a-zA-Z0-9]+", "_", local_name).strip("_").upper()
    if name and name[0].isdigit():
        name = "_" + name
    return name


def _enum_sig_key(model_type: str, values: list[str]) -> str:
    """Build the lookup key for ``_ENUM_MAPPING``.

    Args:
        model_type: ACI model type string.
        values:     Sorted list of canonical enum values.

    Returns:
        Composite key string matching what ``generate_enums.py`` writes.
    """
    return model_type + "||" + "|".join(values)


_ACI_CLASS_RE = re.compile(r"^([a-z]+)(.+)$")


def _aci_to_dot_class(aci_class: str) -> str:
    """Convert an ACI class name to dotted-class notation used in scopemeta.

    Args:
        aci_class: ACI class name, e.g. ``"fvBD"``.

    Returns:
        Dotted notation, e.g. ``"fv.BD"``.
    """
    m = _ACI_CLASS_RE.match(aci_class)
    return f"{m.group(1)}.{m.group(2)}" if m else aci_class


# ── Field line builder ────────────────────────────────────────────────────────


def _bool_field_line(
    py_name: str, alias_arg: str | None, secure: bool, default: Any, desc_arg: str | None
) -> str:
    """Render the field line for a ``bool`` property."""
    default_repr = str(bool(default))
    if alias_arg or secure or desc_arg:
        args: list[str] = [f"default={default_repr}"]
        if alias_arg:
            args.append(alias_arg)
        if secure:
            args.append("repr=False")
        if desc_arg:
            args.append(desc_arg)
        return f"{py_name}: bool = Field({', '.join(args)})"
    return f"{py_name}: bool = {default_repr}"


def _enum_field_line(
    py_name: str,
    prop: dict[str, Any],
    enum_mapping: dict[str, str],
    alias_arg: str | None,
    secure: bool,
    default: Any,
    desc_arg: str | None,
) -> str:
    """Render the field line for an enum property (StrEnum, Literal fallback)."""
    model_type: str = prop.get("model_type", "")
    values = prop.get("values", [])
    key = _enum_sig_key(model_type, values)
    enum_cls = enum_mapping.get(key, "")
    if enum_cls:
        default_member = _enum_member(default) if default is not None else _enum_member(values[0])
        if alias_arg or secure or desc_arg:
            args = [f"default={enum_cls}.{default_member}"]
            if alias_arg:
                args.append(alias_arg)
            if secure:
                args.append("repr=False")
            if desc_arg:
                args.append(desc_arg)
            return f"{py_name}: {enum_cls} = Field({', '.join(args)})"
        return f"{py_name}: {enum_cls} = {enum_cls}.{default_member}"
    # Fallback: shouldn't happen if generate_enums ran first
    return _literal_field_line(py_name, prop, alias_arg, secure, default, desc_arg)


def _literal_field_line(
    py_name: str,
    prop: dict[str, Any],
    alias_arg: str | None,
    secure: bool,
    default: Any,
    desc_arg: str | None,
) -> str:
    """Render the field line for a ``Literal`` property (legacy fallback)."""
    values = prop.get("values", [])
    literal_args = ", ".join(repr(v) for v in values)
    default_str = repr(default) if default is not None else repr(values[0])
    if alias_arg or secure or desc_arg:
        args = [f"default={default_str}"]
        if alias_arg:
            args.append(alias_arg)
        if secure:
            args.append("repr=False")
        if desc_arg:
            args.append(desc_arg)
        return f"{py_name}: Literal[{literal_args}] = Field({', '.join(args)})"
    return f"{py_name}: Literal[{literal_args}] = {default_str}"


def _int_field_line(
    py_name: str,
    prop: dict[str, Any],
    alias_arg: str | None,
    secure: bool,
    default: Any,
    desc_arg: str | None,
) -> str:
    """Render the field line for an ``int`` property (with ge/le constraints)."""
    field_args: list[str] = []
    if (ge := prop.get("ge")) is not None:
        field_args.append(f"ge={ge}")
    if (le := prop.get("le")) is not None and le > 0:
        field_args.append(f"le={le}")
    if alias_arg:
        field_args.append(alias_arg)
    if secure:
        field_args.append("repr=False")
    if desc_arg:
        field_args.append(desc_arg)
    default_val = default if default is not None else 0
    if field_args:
        return f"{py_name}: Annotated[int, Field({', '.join(field_args)})] = {default_val}"
    return f"{py_name}: int = {default_val}"


def _str_field_line(
    py_name: str,
    prop_name: str,
    prop: dict[str, Any],
    alias_arg: str | None,
    is_aliased: bool,
    secure: bool,
    is_naming: bool,
    default: Any,
    desc_arg: str | None,
) -> str:
    """Render the field line for a ``str`` property (constraints, patterns)."""
    str_args: list[str] = []

    validate_as = prop.get("validate_as")
    min_length = prop.get("min_length")
    max_length = prop.get("max_length")
    pattern = prop.get("pattern")

    if min_length is not None and min_length > 0:
        str_args.append(f"min_length={min_length}")
    if max_length is not None:
        str_args.append(f"max_length={max_length}")

    if validate_as == "ip":
        str_args.append(f"pattern={_IP_PATTERN!r}")
    elif validate_as == "mac":
        str_args.append(f"pattern={_MAC_PATTERN!r}")
    elif pattern:
        str_args.append(f"pattern={pattern!r}")

    if alias_arg:
        str_args.append(alias_arg)
    if secure:
        str_args.append("repr=False")
    if desc_arg:
        str_args.append(desc_arg)

    type_ann = f"Annotated[str, Field({', '.join(str_args)})]" if str_args else "str"

    if is_naming:
        return f"{py_name}: {type_ann}"

    default_str = repr(default) if default is not None else '""'
    # Plain str with only an alias and no other constraints: use Field() form
    if is_aliased and not (min_length or max_length or pattern or validate_as or secure):
        args = [f"default={default_str}", f"alias={prop_name!r}"]
        if desc_arg:
            args.append(desc_arg)
        return f"{py_name}: str = Field({', '.join(args)})"
    return f"{py_name}: {type_ann} = {default_str}"


def _field_line(
    prop_name: str,
    prop: dict[str, Any],
    enum_mapping: dict[str, str],
    *,
    sm_label: str = "",
    py_name_override: str | None = None,
) -> str:
    """Return a single Pydantic field definition line for a property.

    Uses :func:`best_field_name` to derive a human-readable snake_case Python
    identifier from the JSON schema label (``prop["label"]``) and the scopemeta
    label (*sm_label*).  An ``alias=`` is added whenever the Python name differs
    from the ACI wire name so the wire format is preserved.

    Args:
        prop_name: ACI property name (camelCase).
        prop:      Normalised property dict from sdk_subset.json.
        sm_label:  Scopemeta display label for this property (optional).

    Returns:
        A valid Python field definition string, e.g.::

            name: Annotated[str, Field(min_length=1, max_length=64, pattern=...)]
            arp_flooding: bool = Field(default=False, alias='arpFlood')
            multiDstPktAct: Literal["bd-flood", "drop", "encap-flood"] = "bd-flood"
            from_: str = Field(default='', alias='from')
    """
    python_type = prop["python_type"]
    is_naming = prop.get("is_naming", False)
    secure = prop.get("secure", False)
    default = prop.get("default")

    if py_name_override is not None:
        py_name = py_name_override
    else:
        json_label = prop.get("label", "")
        py_name = best_field_name(prop_name, json_label, sm_label, is_naming=is_naming)
    is_aliased = py_name != prop_name
    alias_arg = f"alias={prop_name!r}" if is_aliased else None
    # Cisco's schema comment, cleaned at extraction — surfaces in IDE hovers,
    # Pydantic errors and Sphinx autodoc.
    desc_arg = f"description={comment!r}" if (comment := prop.get("comment")) else None

    if python_type == "bool":
        return _bool_field_line(py_name, alias_arg, secure, default, desc_arg)
    if python_type == "enum":
        return _enum_field_line(py_name, prop, enum_mapping, alias_arg, secure, default, desc_arg)
    if python_type == "literal":
        return _literal_field_line(py_name, prop, alias_arg, secure, default, desc_arg)
    if python_type == "int":
        return _int_field_line(py_name, prop, alias_arg, secure, default, desc_arg)
    return _str_field_line(
        py_name, prop_name, prop, alias_arg, is_aliased, secure, is_naming, default, desc_arg
    )


def _render_class(
    aci_class: str,
    class_data: dict[str, Any],
    env: Environment,
    inputs: _CodegenInputs,
) -> str:
    """Render one generated class file using the Jinja2 template.

    Prop names are converted to human-readable snake_case using
    :func:`best_field_name`.  The ``_rn_format`` placeholder is updated to
    match whenever a naming prop is renamed.  Models carry data and
    validation only — the write path is the design DSL, so no builder
    methods are emitted.

    Args:
        class_name: ACI class name, e.g. ``"fvBD"``.
        class_data: Entry from sdk_subset.json: ``{"class": {...}, "properties": {...}}``.
        env:        Jinja2 environment pointed at the templates directory.

    Returns:
        Full content of the generated .py file.
    """
    meta = class_data["class"]
    props = class_data["properties"]

    naming_props_aci = meta.get("identified_by", [])
    rn_format = meta.get("rn_format", "")
    contains = sorted(meta.get("contains", []))

    dot_class = _aci_to_dot_class(aci_class)
    sm_class = inputs.sm_labels.get(dot_class, {})

    # Resolve all Python names once, with intra-class collision detection.
    py_names = resolve_py_names(props, sm_class, aci_class)

    def _prop_py_name(aci_prop: str) -> str:
        return py_names.get(aci_prop, propname_to_snake(aci_prop))

    # Rename props in _naming_props and _rn_format
    naming_props_py: list[str] = []
    for p in naming_props_aci:
        py_p = _prop_py_name(p)
        naming_props_py.append(py_p)
        if py_p != p:
            rn_format = rn_format.replace(f"{{{p}}}", f"{{{py_p}}}")

    naming_fields: list[str] = []
    create_only_fields: list[str] = []
    optional_fields: list[str] = []
    enum_imports: set[str] = set()
    secure_props: list[str] = []

    has_aliased_props = False

    for prop_name, prop_data in sorted(props.items()):
        if prop_data.get("secure"):
            secure_props.append(py_names.get(prop_name) or _prop_py_name(prop_name))
        if _prop_py_name(prop_name) != prop_name:
            has_aliased_props = True
        line = _field_line(
            prop_name,
            prop_data,
            inputs.enum_mapping,
            sm_label=sm_class.get(prop_name, ""),
            py_name_override=py_names.get(prop_name),
        )
        if prop_data.get("python_type") == "enum":
            key = _enum_sig_key(prop_data.get("model_type", ""), prop_data.get("values", []))
            enum_cls = inputs.enum_mapping.get(key, "")
            if enum_cls:
                enum_imports.add(enum_cls)
        if prop_data.get("is_naming"):
            naming_fields.append(line)
        elif prop_data.get("create_only"):
            create_only_fields.append(line)
        else:
            optional_fields.append(line)

    needs_literal = any(p["python_type"] == "literal" for p in props.values())
    needs_annotated = any(
        (p["python_type"] == "int" and (p.get("ge") is not None or p.get("le") is not None))
        or (
            p["python_type"] == "str"
            and any(p.get(k) for k in ("min_length", "max_length", "pattern", "validate_as"))
        )
        # secure str/int use Annotated[T, Field(repr=False)]
        or (p.get("secure") and p["python_type"] in ("str", "int"))
        # described str/int use Annotated[T, Field(description=...)]
        or (p.get("comment") and p["python_type"] in ("str", "int"))
        for p in props.values()
    )
    # Field() is needed for aliased props, secure or described fields, or Annotated props
    needs_field = (
        needs_annotated
        or has_aliased_props
        or any(p.get("secure") or p.get("comment") for p in props.values())
    )

    # ── Semantic metadata from 01_extract_classes ──────────────────────────────
    mo_category = meta.get("mo_category", "Regular")
    label = meta.get("label", "")
    # Cisco's class-level schema comment; keep it docstring-safe.
    class_comment = meta.get("comment", "").replace('"""', "'''")
    # The APIC's declared accepted-but-inconsistent states for this class.
    config_issues: dict[str, str] = meta.get("config_issues", {})
    healthy = {"ok", "none", "N/A", "not-applicable"}
    config_issue_display = [
        f"- ``{code}``" + (f" — {desc}" if desc else "")
        for code, desc in sorted(config_issues.items())
        if code not in healthy
    ]
    config_issue_display = [
        line.replace("\\", "\\\\").replace('"""', "'''") for line in config_issue_display
    ]
    write_access = meta.get("write_access", [])
    is_observable = meta.get("is_observable", False)
    is_faultable = meta.get("is_faultable", False)
    is_health_scorable = meta.get("is_health_scorable", False)
    has_stats = meta.get("has_stats", False)

    template = env.get_template("mo_class.py.jinja2")
    return template.render(
        class_name=aci_class,
        rn_format=rn_format,
        naming_props=naming_props_py,
        secure_props=sorted(secure_props),
        contains=contains,
        naming_fields=naming_fields,
        create_only_fields=create_only_fields,
        optional_fields=optional_fields,
        needs_literal=needs_literal,
        needs_annotated=needs_annotated,
        needs_field=needs_field,
        enum_imports=sorted(enum_imports),
        mo_category=mo_category,
        label=label,
        class_comment=class_comment,
        config_issues=config_issues,
        config_issue_display=config_issue_display,
        write_access=write_access,
        is_observable=is_observable,
        is_faultable=is_faultable,
        is_health_scorable=is_health_scorable,
        has_stats=has_stats,
    )


# ── Init file builder ─────────────────────────────────────────────────────────


def _render_init(pkg_map: dict[str, str]) -> str:
    """Render the _generated/__init__.py with a generated _PKG_MAP dict.

    Importing this package triggers lazy-load of all generated classes into
    ``models.base.REGISTRY`` via ``load_all()``, without requiring one import
    line per class.

    Args:
        pkg_map: ``{full_aci_name: class_pkg}`` for all 2222 generated classes.

    Returns:
        Content of the ``__init__.py`` file.
    """
    lines: list[str] = [
        "# ruff: noqa\n",
        "# Generated by niwaki codegen — do not edit manually.\n",
        "# Re-generate: uv run python -m niwaki._codegen.generate\n",
        "from __future__ import annotations\n\n",
        "import importlib\n\n",
        "_PKG_MAP: dict[str, str] = {\n",
    ]
    for full_name in sorted(pkg_map):
        lines.append(f"    {full_name!r}: {pkg_map[full_name]!r},\n")
    lines.append("}\n\n\n")
    lines.append(
        "def load_all() -> None:\n"
        '    """Import all generated modules to populate models.base.REGISTRY.\n\n'
        "    Call explicitly when you need the full REGISTRY populated (e.g. for\n"
        "    :meth:`~niwaki.models.base.ManagedObject.from_apic` on arbitrary types\n"
        "    without having imported them first).\n"
        '    """\n'
        "    for _full_name, _pkg in _PKG_MAP.items():\n"
        '        importlib.import_module(f"niwaki.models._generated.{_pkg}.{_full_name}")\n'
    )
    return "".join(lines)


def _render_pkg_init(pkg: str, classes: dict[str, str]) -> str:
    """Render the ``__init__.py`` for one ACI package subdirectory.

    Provides lazy exports so ``from niwaki.models._generated.fv import BD``
    works without importing every class in the package upfront.

    Args:
        pkg: ACI package name (e.g. ``"fv"``).
        classes: ``{class_name_short: full_aci_name}`` for this package,
            e.g. ``{"BD": "fvBD", "Tenant": "fvTenant"}``.

    Returns:
        Content of ``{pkg}/__init__.py``.
    """
    lines: list[str] = [
        "# ruff: noqa\n",
        f"# Generated ACI package: {pkg}\n",
        "# Generated by niwaki codegen — do not edit manually.\n",
        "from __future__ import annotations\n\n",
        "_CLASSES: dict[str, str] = {\n",
    ]
    for short_name in sorted(classes):
        lines.append(f"    {short_name!r}: {classes[short_name]!r},\n")
    lines.append("}\n\n\n")
    lines.append(
        "def __getattr__(name: str) -> type:\n"
        "    full = _CLASSES.get(name)\n"
        "    if full is None:\n"
        '        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")\n'
        "    from importlib import import_module\n"
        '    mod = import_module(f".{full}", __name__)\n'
        "    cls = getattr(mod, full)\n"
        "    globals()[name] = cls\n"
        "    return cls\n\n\n"
        "__all__ = list(_CLASSES)\n"
    )
    return "".join(lines)


# ── Test data builder ─────────────────────────────────────────────────────────


def _naming_val(prop_data: dict[str, Any]) -> object:
    """Return the test value for a naming prop, keyed on its Python type.

    For ``str`` props: ``_VALID_VAL`` (``"ab"``).
    For ``int`` / ``literal`` props: the ACI schema default (they're not required).

    Args:
        prop_data: Normalised property dict from sdk_subset.json.

    Returns:
        A value that can be passed as a constructor argument for this prop.
    """
    pt = prop_data.get("python_type", "str")
    if pt == "str":
        return _VALID_VAL
    default = prop_data.get("default")
    if pt == "int":
        return default if default is not None else 0
    # literal / bool: use declared default
    return default


def _naming_attrs_str(prop_data: dict[str, Any], py_val: object) -> str:
    """Return the string that ``to_apic()`` emits for a naming prop value.

    Args:
        prop_data: Normalised property dict.
        py_val:    Value used in the Python constructor.

    Returns:
        String representation in ACI attribute format.
    """
    if prop_data.get("python_type") == "bool":
        return "true" if py_val else "false"
    return str(py_val)


def _build_test_data(
    subset: dict[str, dict[str, Any]], sm_labels: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Extract parametrised test fixtures from the full class + property data.

    Written to tests/models/_test_data.json and consumed by
    tests/models/test_all_generated.py at pytest collection time.

    Fixture categories:

    - ``no_naming``  : class names with no identifying property
    - ``naming``     : ``[cls, python_prop, min_l, max_l, has_pattern]``
      Only ``str`` naming props are included here — ``int`` / ``literal``
      props have schema defaults and are not required.
    - ``rn``         : ``[cls, naming_kwargs, expected_rn]``
    - ``enums``      : ``[cls, naming_kwargs, prop, values, default]``
    - ``bools``      : ``[cls, naming_kwargs, prop, expected]``
    - ``surgical``   : ``[cls, naming_kwargs, expected_aci_attrs]``

    ``naming_kwargs`` uses Python attribute names (e.g. ``from_``) and only
    includes ``str`` naming props (non-str props use their schema defaults).
    ``expected_aci_attrs`` uses original ACI names (e.g. ``from``) and
    reflects the actual values ``to_apic()`` will emit (incl. defaults for
    non-str naming props).

    Args:
        subset: Full sdk_subset.json content, keyed by ACI class name.
        sm_labels: Scopemeta display labels (dotted class → {prop: label}).

    Returns:
        Dict mapping fixture category names to lists of test parameter tuples.
    """
    no_naming: list[str] = []
    naming: list[list[object]] = []
    rn: list[list[object]] = []
    enums: list[list[object]] = []
    bools: list[list[object]] = []
    surgical: list[list[object]] = []

    for aci_class, cls_data in sorted(subset.items()):
        meta = cls_data["class"]
        props = cls_data["properties"]

        identified_by = meta.get("identified_by", [])
        rn_format = meta.get("rn_format", "")

        # Per-class resolved names (collision-safe)
        dot_class = _aci_to_dot_class(aci_class)
        sm_class = sm_labels.get(dot_class, {})
        py_names = resolve_py_names(props, sm_class, aci_class)

        def _py(p: str, _pn: dict[str, str] = py_names) -> str:
            return _pn.get(p, propname_to_snake(p))

        # Skip classes whose naming prop has a special validator (IP, MAC)
        # since "test" would fail those patterns.
        skip_class = any(p not in props or props[p].get("validate_as") for p in identified_by)

        if not identified_by:
            no_naming.append(aci_class)

        if skip_class:
            continue

        # naming_kwargs: only str naming props passed explicitly.
        # int/literal naming props use their schema defaults → not in kwargs.
        naming_kwargs: dict[str, Any] = {}
        for p in identified_by:
            if p not in props:
                continue
            prop_data = props[p]
            if prop_data.get("python_type", "str") == "str":
                naming_kwargs[_py(p)] = _VALID_VAL

        # ── Naming constraint tests (str naming props only) ─────────────────
        for p in identified_by:
            if p not in props:
                continue
            prop_data = props[p]
            if prop_data.get("python_type", "str") != "str":
                continue
            min_l = prop_data.get("min_length") or 0
            max_l = prop_data.get("max_length")  # None when unconstrained
            pattern = prop_data.get("pattern") or ""
            # Only mark has_pat=True when the pattern actually excludes spaces
            # (some ACI patterns allow spaces, e.g. ^[a-zA-Z0-9=!#$%@ ]+$)
            has_pat = bool(prop_data.get("validate_as")) or (bool(pattern) and " " not in pattern)
            naming.append([aci_class, _py(p), min_l, max_l, has_pat])

        # ── RN test ─────────────────────────────────────────────────────────
        # rn_format from sdk_subset uses ACI names; apply prop renaming
        # then substitute each naming prop's test value.
        rn_fmt_py = rn_format
        for p in identified_by:
            py_p = _py(p)
            if py_p != p:
                rn_fmt_py = rn_fmt_py.replace(f"{{{p}}}", f"{{{py_p}}}")

        expected_rn = rn_fmt_py
        for p in identified_by:
            if p not in props:
                continue
            prop_data = props[p]
            py_p = _py(p)
            val = _naming_val(prop_data)
            val_s = _naming_attrs_str(prop_data, val)
            expected_rn = expected_rn.replace(f"{{{py_p}}}", val_s)
        rn.append([aci_class, naming_kwargs, expected_rn])

        # ── Enums + bools (non-naming configurable props) ───────────────────
        for prop_name, prop_data in sorted(props.items()):
            if prop_data.get("is_naming"):
                continue
            if prop_data["python_type"] in ("literal", "enum"):
                enums.append(
                    [
                        aci_class,
                        naming_kwargs,
                        _py(prop_name),
                        prop_data.get("values", []),
                        prop_data.get("default"),
                    ]
                )
            elif prop_data["python_type"] == "bool":
                bools.append(
                    [
                        aci_class,
                        naming_kwargs,
                        _py(prop_name),
                        prop_data.get("default", False),
                    ]
                )

        # ── Surgical to_apic() ──────────────────────────────────────────────
        # expected_attrs: ACI names → string-serialised values that to_apic()
        # emits (including non-str naming props at their schema defaults).
        expected_attrs: dict[str, str] = {}
        for p in identified_by:
            if p not in props:
                continue
            prop_data = props[p]
            val = _naming_val(prop_data)
            expected_attrs[p] = _naming_attrs_str(prop_data, val)
        surgical.append([aci_class, naming_kwargs, expected_attrs])

    return {
        "no_naming": no_naming,
        "naming": naming,
        "rn": rn,
        "enums": enums,
        "bools": bools,
        "surgical": surgical,
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Read sdk_subset.json and write one .py file per class to _generated/.

    Also writes:
    - ``_generated/__init__.py`` (pkgutil auto-discover)
    - ``tests/models/_test_data.json`` (parametrised test fixtures)

    Stale .py files from previous runs are removed before writing new ones.
    Requires ``enum_mapping.json`` (produced by ``generate_enums.py``) to be
    present so that enum fields are rendered as typed ``StrEnum`` references.
    """
    if not SUBSET_FILE.exists():
        print(
            f"ERROR: {SUBSET_FILE} not found — run data/scripts/03_build_subset.py first",
            file=sys.stderr,
        )
        sys.exit(1)

    enum_mapping: dict[str, str] = {}
    if MAPPING_FILE.exists():
        enum_mapping = json.loads(MAPPING_FILE.read_text())
    else:
        print(
            f"WARNING: {MAPPING_FILE} not found — run generate_enums.py first; "
            "enum fields will fall back to Literal types",
            file=sys.stderr,
        )

    sm_labels: dict[str, dict[str, str]] = {}
    if SM_LABELS_FILE.exists():
        sm_labels = json.loads(SM_LABELS_FILE.read_text())
    else:
        print(
            f"WARNING: {SM_LABELS_FILE} not found — scopemeta labels unavailable; "
            "field names will rely on JSON schema labels only",
            file=sys.stderr,
        )

    subset: dict[str, dict[str, Any]] = json.loads(SUBSET_FILE.read_text())

    child_map: dict[str, dict[str, str]] = {}
    rs_target_prop: dict[str, str] = {}
    class_pkg: dict[str, str] = {}
    try:
        from niwaki.domain import _child_map

        child_map = _child_map.CHILD_MAP
        rs_target_prop = _child_map.RS_TARGET_PROP
        class_pkg = _child_map.CLASS_PKG
    except ImportError:
        print(
            "WARNING: niwaki.domain._child_map not importable — "
            "run generate_domain.py first; builder methods will be omitted",
            file=sys.stderr,
        )

    inputs = _CodegenInputs(
        enum_mapping=enum_mapping,
        sm_labels=sm_labels,
        child_map=child_map,
        rs_target_prop=rs_target_prop,
        class_pkg=class_pkg,
    )

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Remove stale flat .py class files from old structure.
    for stale in OUTPUT_DIR.glob("*.py"):
        if stale.name != "__init__.py":
            stale.unlink()

    # Remove stale package subdirectories (keep enums/ which generate_enums.py owns).
    for item in OUTPUT_DIR.iterdir():
        if item.is_dir() and item.name not in ("enums", "__pycache__"):
            shutil.rmtree(item)

    # Collect {pkg: {class_name_short: full_name}} and build _PKG_MAP.
    pkg_classes: dict[str, dict[str, str]] = defaultdict(dict)
    pkg_map: dict[str, str] = {}

    generated: list[str] = []
    for aci_class, class_data in sorted(subset.items()):
        pkg = class_data["class"].get("class_pkg", "")
        short_name = class_data["class"].get("class_name", "")

        if not pkg:
            print(f"WARNING: no class_pkg for {aci_class} — skipping", file=sys.stderr)
            continue

        pkg_dir = OUTPUT_DIR / pkg
        pkg_dir.mkdir(exist_ok=True)

        content = _render_class(aci_class, class_data, env, inputs)
        (pkg_dir / f"{aci_class}.py").write_text(content)

        pkg_classes[pkg][short_name] = aci_class
        pkg_map[aci_class] = pkg
        generated.append(aci_class)

    print(f"Generated {len(generated)} class files in {len(pkg_classes)} packages  →  {OUTPUT_DIR}")

    # Per-package __init__.py.
    for pkg, classes in sorted(pkg_classes.items()):
        pkg_init = OUTPUT_DIR / pkg / "__init__.py"
        pkg_init.write_text(_render_pkg_init(pkg, classes))
    print(f"  + {len(pkg_classes)} package __init__.py files")

    (OUTPUT_DIR / "__init__.py").write_text(_render_init(pkg_map))
    print("  + __init__.py (_PKG_MAP + load_all)")

    test_data = _build_test_data(subset, sm_labels)
    TEST_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEST_DATA_FILE.write_text(json.dumps(test_data, indent=2))
    print(
        f"  + {TEST_DATA_FILE.name}  "
        f"({len(test_data['naming'])} naming / "
        f"{len(test_data['enums'])} enum / "
        f"{len(test_data['bools'])} bool entries)"
    )


if __name__ == "__main__":
    main()
