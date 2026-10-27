"""Raw APIC JSON has a public name: ``niwaki.types``.

Everything the APIC sends and receives is JSON — ``{"fvTenant": {"attributes":
{...}, "children": [...]}}``, the ``imdata`` list, a snapshot tree.  Typing that
as ``dict[str, Any]`` switches the type checker off for every value read out of
it.  ``niwaki.types`` gives the shape a name instead, as PEP 695 ``type``
aliases (also reachable, as the very same objects, from the internal
``niwaki._json``):

* ``JsonScalar`` — ``str | int | float | bool | None``
* ``JsonValue`` — ``JsonScalar | list[JsonValue] | dict[str, JsonValue]``
  (recursive)
* ``JsonObject`` — ``dict[str, JsonValue]``

The signature contract has two sides, because ``dict`` is invariant:

* **Producers** (``to_apic``, ``to_payload``, ``ApicSession.get``,
  ``Snapshot.tree``) return exactly ``JsonObject`` — the caller narrows.
* **Consumers** (``from_apic``, ``from_event``, ``parse_imdata``,
  ``from_payload``, every ``post_mo``) take ``Mapping[str, object]``, and
  ``parse_envelopes`` an ``Iterable`` of it, so an unannotated literal, a
  ``dict[str, dict[str, str]]`` or a payload composed of producer outputs is
  accepted as is.

This module pins:

1. **Runtime** — ``niwaki.types`` exists, exports the three aliases, they are
   real :class:`typing.TypeAliasType` objects with the shapes above, identical
   to ``niwaki._json``'s, and pydantic can validate against them (which also
   exercises the recursion).
2. **Consumer signatures, runtime side** — read from the annotations, so a
   regression to ``JsonObject`` or ``dict[str, Any]`` fails here.
3. **Static side** — ``tests/meta/json_type_probes.py`` holds realistic user
   code and ``assert_type`` pins; pyright checks it in
   ``scripts/checks.sh --fast`` and :func:`test_probes_type_check_under_mypy`
   runs mypy on it.
4. **Documentation** — the module is in the API reference and the public-path
   policy, and Sphinx no longer silences the private alias path.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import typing
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import GenericAlias, ModuleType, NoneType, UnionType

import pytest
from pydantic import ConfigDict, TypeAdapter, ValidationError

_ROOT = Path(__file__).resolve().parents[1]
_PROBES = _ROOT / "tests" / "meta" / "json_type_probes.py"
_ALIASES = ("JsonScalar", "JsonValue", "JsonObject")


def _types_module() -> ModuleType:
    """Import ``niwaki.types`` inside the test, so this module always collects."""
    return importlib.import_module("niwaki.types")


def _alias(name: str) -> typing.TypeAliasType:
    alias = getattr(_types_module(), name)
    assert isinstance(alias, typing.TypeAliasType), f"{name} is not a PEP 695 alias"
    return alias


def _flatten_union(tp: object) -> set[object]:
    """Return the members of a union, expanding nested unions and aliases.

    ``JsonValue`` may be spelled ``JsonScalar | list[...] | dict[...]`` or with
    the scalars inlined; both are the same type and both must pass.
    """
    if isinstance(tp, typing.TypeAliasType):
        return _flatten_union(tp.__value__)
    if typing.get_origin(tp) is typing.Union or isinstance(tp, UnionType):
        members: set[object] = set()
        for arg in typing.get_args(tp):
            members |= _flatten_union(arg)
        return members
    return {tp}


# ── 1. the public module and its aliases ─────────────────────────────────────


def test_public_module_exports_exactly_the_three_aliases() -> None:
    module = _types_module()
    assert set(getattr(module, "__all__", ())) == set(_ALIASES)
    missing = [name for name in _ALIASES if not hasattr(module, name)]
    assert not missing, f"niwaki.types is missing {missing}"


@pytest.mark.parametrize("name", _ALIASES)
def test_public_aliases_are_the_internal_ones(name: str) -> None:
    """One definition, two import paths — never two lookalike aliases."""
    internal = importlib.import_module("niwaki._json")
    assert getattr(_types_module(), name) is getattr(internal, name)


@pytest.mark.parametrize("name", _ALIASES)
def test_aliases_are_pep695_type_statements(name: str) -> None:
    alias = _alias(name)
    assert alias.__name__ == name
    assert alias.__module__ in {"niwaki.types", "niwaki._json"}


def test_json_scalar_is_the_json_scalar_set() -> None:
    assert _flatten_union(_alias("JsonScalar")) == {str, int, float, bool, NoneType}


def test_json_value_is_recursive() -> None:
    json_value = _alias("JsonValue")
    assert _flatten_union(json_value) == {
        str,
        int,
        float,
        bool,
        NoneType,
        GenericAlias(list, (json_value,)),
        GenericAlias(dict, (str, json_value)),
    }


def test_json_object_is_a_string_keyed_mapping_of_values() -> None:
    json_value = _alias("JsonValue")
    assert _alias("JsonObject").__value__ == GenericAlias(dict, (str, json_value))


# ── the aliases describe real APIC payloads (pydantic follows the recursion) ─

_ENVELOPE = {
    "fvTenant": {
        "attributes": {"dn": "uni/tn-prod", "name": "prod", "descr": ""},
        "children": [
            {"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}, "children": []}},
        ],
    }
}


def _strict_adapter(name: str) -> TypeAdapter[object]:
    """Return a strict pydantic adapter for the alias *name*.

    Strict mode: no lax coercion (``b"x"`` → ``"x"``, ``{1, 2}`` → ``[1, 2]``)
    may hide a non-JSON value behind the alias.
    """
    alias = _alias(name)
    # The alias is only known at runtime here (imported inside the test).
    return TypeAdapter(alias, config=ConfigDict(strict=True))  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "payload",
    [
        _ENVELOPE,
        {},
        {"imdata": [], "totalCount": "0"},
        {"a": None, "b": True, "c": 1, "d": 1.5, "e": [[[{"deep": []}]]]},
    ],
    ids=["apic-envelope", "empty", "empty-imdata", "every-scalar-and-deep-nesting"],
)
def test_json_object_accepts_json_documents(payload: object) -> None:
    assert _strict_adapter("JsonObject").validate_python(payload) == payload


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"a": object()},
        {1: "non-string key"},
        {"a": {1, 2}},
        {"a": b"bytes"},
        {"nested": {"deep": [object()]}},
    ],
    ids=["list-at-top", "opaque-object", "int-key", "set", "bytes", "deep-opaque"],
)
def test_json_object_rejects_non_json(payload: object) -> None:
    adapter = _strict_adapter("JsonObject")
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


# ── 2. consumers take a read-only mapping ────────────────────────────────────


def _param_annotation(fn: Callable[..., object], param: str) -> object:
    """Evaluate the declared annotation of *param* on *fn*.

    The SDK uses ``from __future__ import annotations``, so annotations are
    strings; they are evaluated against the function's module namespace plus
    the aliases and the ``collections.abc`` names (which the module may import
    under ``TYPE_CHECKING`` only).
    """
    raw = inspect.signature(fn).parameters[param].annotation
    if not isinstance(raw, str):
        return raw
    module = _types_module()
    namespace: dict[str, object] = {
        **vars(typing),
        **vars(sys.modules[fn.__module__]),
        "Mapping": Mapping,
        "Iterable": Iterable,
        **{name: getattr(module, name) for name in _ALIASES},
    }
    return eval(raw, namespace)


_READ_ONLY_OBJECT = GenericAlias(Mapping, (str, object))


def _consumer(label: str) -> tuple[Callable[..., object], str, object]:
    """Return ``(function, JSON parameter, expected annotation)`` for *label*."""
    from niwaki.design import from_payload
    from niwaki.models.base import ManagedObject
    from niwaki.transport import ApicSession, AsyncApicSession, AsyncMoWriter, MoWriter
    from niwaki.utils import parse_imdata
    from niwaki.utils.response import parse_envelopes

    consumers: dict[str, tuple[Callable[..., object], str, object]] = {
        "parse_imdata": (parse_imdata, "data", _READ_ONLY_OBJECT),
        "parse_envelopes": (
            parse_envelopes,
            "envelopes",
            GenericAlias(Iterable, (_READ_ONLY_OBJECT,)),
        ),
        "ManagedObject.from_apic": (ManagedObject.from_apic, "data", _READ_ONLY_OBJECT),
        "ManagedObject.from_event": (ManagedObject.from_event, "data", _READ_ONLY_OBJECT),
        "from_payload": (from_payload, "payload", _READ_ONLY_OBJECT),
        "ApicSession.post_mo": (ApicSession.post_mo, "payload", _READ_ONLY_OBJECT),
        "AsyncApicSession.post_mo": (AsyncApicSession.post_mo, "payload", _READ_ONLY_OBJECT),
        "MoWriter.post_mo": (MoWriter.post_mo, "payload", _READ_ONLY_OBJECT),
        "AsyncMoWriter.post_mo": (AsyncMoWriter.post_mo, "payload", _READ_ONLY_OBJECT),
    }
    return consumers[label]


@pytest.mark.parametrize(
    "label",
    [
        "parse_imdata",
        "parse_envelopes",
        "ManagedObject.from_apic",
        "ManagedObject.from_event",
        "from_payload",
        "ApicSession.post_mo",
        "AsyncApicSession.post_mo",
        "MoWriter.post_mo",
        "AsyncMoWriter.post_mo",
    ],
)
def test_raw_json_consumers_take_a_read_only_mapping(label: str) -> None:
    """A function that *reads* an envelope accepts any string-keyed mapping.

    ``JsonObject`` would be wrong here: ``dict`` is invariant, so it rejects
    ``dict[str, dict[str, str]]``, an unannotated nested literal, or
    ``{"imdata": session.get(...)}``.  The function proves the shape at runtime.
    """
    fn, param, expected = _consumer(label)
    declared = _param_annotation(fn, param)
    assert declared == expected, f"{label}({param}=...) is annotated {declared!r}"


# ── 3. the static probes pass mypy too ───────────────────────────────────────


def test_probes_type_check_under_mypy() -> None:
    """``tests/meta/json_type_probes.py`` is clean under mypy strict.

    pyright covers the same file through ``scripts/checks.sh --fast``; mypy is
    the SDK's reference checker for ``src/``, and the two disagree often enough
    on variance and overloads that both must accept the probes.
    """
    from mypy import api

    stdout, stderr, status = api.run(
        ["--config-file", str(_ROOT / "pyproject.toml"), "--no-error-summary", str(_PROBES)]
    )
    assert status == 0, f"mypy rejects the JSON type probes:\n{stdout}{stderr}"


# ── 4. documented as public ──────────────────────────────────────────────────


def test_api_reference_documents_the_types_module() -> None:
    pages = sorted((_ROOT / "docs" / "reference" / "api").glob("*.md"))
    text = "\n".join(page.read_text(encoding="utf-8") for page in pages)
    assert re.search(r"^\.\. automodule:: niwaki\.types$", text, re.MULTILINE), (
        "docs/reference/api has no 'automodule:: niwaki.types' directive"
    )


def test_versioning_policy_lists_the_types_module() -> None:
    policy = (_ROOT / "docs" / "project" / "versioning.md").read_text(encoding="utf-8")
    assert re.search(r"^import niwaki\.types\b", policy, re.MULTILINE), (
        "docs/project/versioning.md does not publish niwaki.types as public"
    )


def test_sphinx_no_longer_silences_the_private_alias_path() -> None:
    """With a public home, a reference to ``niwaki._json.*`` is a doc bug."""
    conf = (_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    assert "niwaki._json" not in conf
    assert r"niwaki\._json" not in conf
