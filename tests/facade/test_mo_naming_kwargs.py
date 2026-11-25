"""``mo()`` and keyword navigation edges accept naming properties only — anything else is refused.

The defect this module pins: the facade built a model from whatever keywords
it was given and kept **only its RN**.  Every keyword that is not a naming
property was therefore validated, accepted and dropped::

    epg.mo(fvRsCustQosPol, name="gold").dn
    # 'uni/tn-p/ap-a/epg-e/rscustQosPol' — "gold" ignored
    aci.tenant("p").bd("w").subnet(subnet="10.0.0.1/24", scope="public").dn
    # 'uni/tn-p/BD-w/subnet-[10.0.0.1/24]' — scope ignored
    aci.fabric().fabric_port_port(description="x").dn
    # neither a Query nor an error: the node of an all-defaults port config

``.delete()`` on such a node removes the object the caller did *not* mean (the
relation, the default port config) while they believe they addressed the one
they described.  The fixed-RN navigation edges already refuse an argument
(``tests/facade/test_fixed_rn_navigation.py``); this module closes the same
hole on the two remaining doors, ``node.mo(cls, **kwargs)`` and the keyword
navigation edges that go through it (``bd.subnet(subnet=...)``,
``fabric.fabric_port_port(node=...)``).

The rule (sync and async identical — the nodes are twins):

* **Accepted keys** — exactly the *naming properties* of the class
  (``cls._naming_props``), spelled either with the Python name or with the
  wire alias (``subnet`` or ``ip`` for ``fvSubnet``; ``selector_type`` or
  ``type`` for ``infraLeafS``).  The two spellings are unambiguous: no other
  field of any generated class answers to either (measured, guarded below).
* **Refusal** — any other key raises :class:`TypeError`: a configurable
  property (``scope``), a real field that is not a naming prop (``name`` on a
  subnet, ``children``), the wire alias of one, a typo, and the Rs target
  prop (``name`` / ``tnQosCustomPolName`` on ``fvRsCustQosPol``).  The check
  covers *every* key of the call — a refused key next to valid naming props is
  still refused — and runs **before** any validation of the values and before
  any request: a refused call never reaches the client (the sweeps build nodes
  without a session, so a request would be an :class:`AttributeError`, not the
  :class:`TypeError`).  A typo is a :class:`TypeError` too, no longer the
  :class:`~pydantic.ValidationError` the model's own construction guard raised.
* **Message** — names the class and the offending key(s).  For a class with
  naming props it also lists them (so the fix is in the message); for a
  fixed-RN class (no naming prop — nothing can be named) it says
  ``takes no name``, the wording of the fixed-RN navigation refusal.
* **Whose words** — a refusal speaks the caller's language: reached through
  an edge it names the edge as called (``subnet(...) takes naming properties
  only…``, like the fixed-RN sibling's ``custom_qos_policy() takes no name…``)
  and never ``mo(fvSubnet)``; reached through ``mo()`` it keeps saying
  ``mo(fvSubnet)``.  The ``name`` edges (``bd("web", arp_flooding=True)``) are
  no exception: the same message, not Python's own ``_nav_name() got an
  unexpected keyword argument`` with a private function name in it.
* **One spelling per naming prop** — giving the same naming property twice,
  by its Python name *and* its wire alias (``subnet=…, ip=…``), is refused
  whatever the order and the values (the alias used to win silently): the
  message names the prop and both spellings.
* **Keys only** — a refusal echoes keys, never values (a refused call may
  carry a secret that must not end up in a traceback).
* **A non-empty kwargs call with no naming prop at all** (``description="x"``
  on a port config whose naming props all have defaults) is refused too — it
  used to fall back to the defaults.
* **Nominal paths are unchanged** — naming props by either spelling reach the
  same DN as before; ``node.mo(fixedRnClass)`` with no argument still works; a
  kwargs edge called empty is still a query; an *invalid value of a naming
  prop* is still a :class:`~pydantic.ValidationError` (the model's job).

The sweeps cover every generated class (``mo()``) and every reachable
navigation edge of ``CHILD_MAP`` (≈7 300 keyword edges), both flavours, with
valid naming values derived from the models themselves.  Navigation is local,
so nodes are built without a session.  The static side is out of this module's
scope: the typed stubs already make a non-naming keyword on an edge a type
error, and ``mo()`` is typed ``**naming_kwargs: object``.
"""

from __future__ import annotations

import enum
import importlib
import inspect
import re
import typing
from collections.abc import Callable, Iterator, Sequence
from functools import cache, partial
from typing import TYPE_CHECKING, Literal, cast

import pytest
from pydantic import ValidationError

from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki._codegen.generate_domain import _node_api_names, _root_api_names
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG
from niwaki.models._generated.fabric.fabricPortConfig import fabricPortConfig
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvRsCustQosPol import fvRsCustQosPol
from niwaki.models._generated.fv.fvSubnet import fvSubnet
from niwaki.models._generated.infra.infraLeafS import infraLeafS

if TYPE_CHECKING:
    from niwaki.models.base import ManagedObject

type Flavour = Literal["sync", "async"]
type Client = Niwaki | AsyncNiwaki
type AnyNode = NiwakiNode[ManagedObject] | AsyncNiwakiNode[ManagedObject]
type Model = type[ManagedObject]
type EdgeKind = Literal["fixed", "name", "kwargs"]
type Probe = tuple[str, object]

FLAVOURS: tuple[Flavour, ...] = ("sync", "async")

_BOGUS = "bogus_property"
_EPG = "uni/tn-prod/ap-shop/epg-web"
_BD = "uni/tn-prod/BD-web"


# ── Model helpers ─────────────────────────────────────────────────────────────


@cache
def _model(aci_class: str) -> Model:
    module = importlib.import_module(f"niwaki.models._generated.{CLASS_PKG[aci_class]}.{aci_class}")
    return cast("Model", getattr(module, aci_class))


def _naming(cls: Model) -> list[str]:
    """The naming properties of *cls*, by Python name — empty for a fixed-RN class."""
    return cls._naming_props  # pyright: ignore[reportPrivateUsage]


def _rn_format(cls: Model) -> str:
    return cls._rn_format  # pyright: ignore[reportPrivateUsage]


def _alias(cls: Model, prop: str) -> str | None:
    """The wire alias of field *prop*, or ``None`` when its wire name is its Python name."""
    alias = cls.model_fields[prop].validation_alias
    return alias if isinstance(alias, str) else None


@cache
def _all_classes() -> tuple[str, ...]:
    return tuple(sorted(CLASS_PKG))


@cache
def _fixed_classes() -> tuple[str, ...]:
    return tuple(name for name in _all_classes() if not _naming(_model(name)))


@cache
def _named_classes() -> tuple[str, ...]:
    return tuple(name for name in _all_classes() if _naming(_model(name)))


def _foreign_probes(cls: Model) -> list[Probe]:
    """Every key *cls* must refuse: each non-naming field by name and wire alias, plus a typo.

    The values are the fields' own defaults (``"x"`` for a required one), so a
    call that went through would be *valid* — the refusal can only be about the
    key, never about the value.
    """
    naming = set(_naming(cls))
    probes: list[Probe] = []
    for name, field in cls.model_fields.items():
        if name in naming:
            continue
        value: object = "x" if field.is_required() else field.get_default(call_default_factory=True)
        probes.append((name, value))
        if (alias := _alias(cls, name)) is not None:
            probes.append((alias, value))
    probes.append((_BOGUS, "x"))
    return probes


def _few_foreign_probes(cls: Model) -> list[Probe]:
    """The edge sweeps' economy: one real field, one wire alias, ``children`` and the typo."""
    probes = _foreign_probes(cls)
    real = [probe for probe in probes if probe[0] not in {"children", _BOGUS}]
    wire_spelled = [probe for probe in real if probe[0] not in cls.model_fields]
    picked = real[:1] + wire_spelled[:1]
    picked += [probe for probe in probes if probe[0] in {"children", _BOGUS}]
    return list({probe[0]: probe for probe in picked}.values())


# Candidate values for a naming property, tried in order: the field's own enum
# members / literals first, then strings of the shapes the schema's patterns
# ask for (vlan encaps, IPs, MACs), then small integers.
_STRINGS: tuple[str, ...] = (
    "vlan-100",
    "vlan-200",
    "uni/infra",
    "ipv4",
    "tcp",
    "unspecified",
    "1.1.1.2",
    "pol",
    "x1",
    "10.0.0.1/24",
    "10.0.0.1",
    "00:11:22:33:44:55",
    "1",
    "uni/tn-x",
    "1.1.1.1",
    "ff02::1",
)
_INTS: tuple[int, ...] = (101, 1, 2, 10, 100, 5, 0, 4, 20, 1000)


def _annotation_values(annotation: object) -> Iterator[object]:
    """Enum members and literals an annotation spells out (through unions and ``Annotated``)."""
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        yield from (member.value for member in annotation)
    if typing.get_origin(annotation) is Literal:
        yield from typing.get_args(annotation)
        return
    for arg in typing.get_args(annotation):
        yield from _annotation_values(arg)


@cache
def _valid_naming(aci_class: str) -> tuple[tuple[str, object], ...] | None:
    """Python-named values that make every naming prop of the class valid, or ``None``.

    Derived from the model itself, one property at a time: the first candidate
    the property's own validation accepts (a violation counts only when it is
    reported on that property — the others may still be missing).  The whole
    set is then validated together; ``None`` when no candidate fits.
    """
    cls = _model(aci_class)
    chosen: dict[str, object] = {}
    for prop in _naming(cls):
        field = cls.model_fields[prop]
        spellings = {prop, _alias(cls, prop) or prop}
        for value in (*_annotation_values(field.annotation), *_STRINGS, *_INTS):
            try:
                cls.model_validate({**chosen, prop: value})
            except ValidationError as error:
                if any(issue["loc"] and issue["loc"][0] in spellings for issue in error.errors()):
                    continue
            chosen[prop] = value
            break
        else:
            return None
    try:
        cls.model_validate(chosen)
    except ValidationError:
        return None
    return tuple(chosen.items())


def _naming_kwargs(cls: Model, *, wire: bool = False) -> dict[str, object]:
    """Valid naming keywords for *cls*, by Python name — or by wire alias where one exists."""
    values = _valid_naming(cls.__name__)
    assert values is not None, f"no valid naming values derivable for {cls.__name__}"
    if not wire:
        return dict(values)
    return {(_alias(cls, prop) or prop): value for prop, value in values}


def _expected_rn(cls: Model) -> str:
    """The RN the model itself computes from the derived naming values."""
    return cls.model_validate(_naming_kwargs(cls)).rn


# ── Node and edge helpers ─────────────────────────────────────────────────────


def _client(flavour: Flavour) -> Client:
    """A client that never connects: navigation is local, no session needed."""
    if flavour == "sync":
        return Niwaki("https://apic.invalid", "admin", "secret")
    return AsyncNiwaki("https://apic.invalid", "admin", "secret")


@pytest.fixture(params=FLAVOURS)
def flavour(request: pytest.FixtureRequest) -> Flavour:
    value: Flavour = request.param
    return value


@pytest.fixture
def client(flavour: Flavour) -> Client:
    return _client(flavour)


def _parent_dn(parent: str) -> str:
    return "uni" if parent == "_root" else "uni/x"


def _node(parent: str, flavour: Flavour) -> AnyNode:
    """A session-less node of *parent*'s class (the root row is ``polUni``'s)."""
    cls = _model("polUni" if parent == "_root" else parent)
    dn = _parent_dn(parent)
    if flavour == "sync":
        return NiwakiNode(cast("Niwaki", None), dn, cls)
    return AsyncNiwakiNode(cast("AsyncNiwaki", None), dn, cls)


def _mo(node: AnyNode, cls: Model, kwargs: dict[str, object]) -> object:
    result: object = node.mo(cls, **kwargs)
    return result


def _reserved(parent: str) -> frozenset[str]:
    """Names a node (or, at the root, the client) API shadows — unreachable edges."""
    return _root_api_names() | _node_api_names() if parent == "_root" else _node_api_names()


def _edges(kind: EdgeKind) -> Iterator[tuple[str, str, str]]:
    """Every reachable ``(parent, edge, child)`` of the *kind*, sorted.

    ``fixed``: no naming prop; ``name``: naming props are exactly ``["name"]``
    (the ``(name="")`` navigator); ``kwargs``: any other naming props (the
    ``(**naming_kwargs)`` navigator).
    """
    for parent in sorted(CHILD_MAP):
        if parent != "_root" and parent not in CLASS_PKG:
            continue
        reserved = _reserved(parent)
        for edge, child in sorted(CHILD_MAP[parent].items()):
            if edge in reserved or child not in CLASS_PKG:
                continue
            naming = _naming(_model(child))
            edge_kind: EdgeKind = (
                "fixed" if not naming else "name" if naming == ["name"] else "kwargs"
            )
            if edge_kind == kind:
                yield parent, edge, child


@cache
def _edges_of(kind: EdgeKind) -> tuple[tuple[str, str, str], ...]:
    return tuple(_edges(kind))


def _navigator(parent: str, edge: str, flavour: Flavour) -> Callable[..., object]:
    navigator: object = getattr(_node(parent, flavour), edge)
    assert callable(navigator)
    return navigator


def _dn(node: object) -> object:
    """The DN of a navigated node (what the typed stubs hide behind ``object``)."""
    return getattr(node, "dn", None)


def _edge_of(owner: object, edge: str) -> Callable[..., object]:
    """``owner.<edge>`` as a plain callable (the typed stubs refuse a foreign keyword)."""
    navigator: object = getattr(owner, edge)
    assert callable(navigator)
    return navigator


# ── The refusal contract ──────────────────────────────────────────────────────


def _refusal_problem(call: Callable[[], object], cls: Model, keys: tuple[str, ...]) -> str | None:
    """``None`` when *call* refuses with a :class:`TypeError` as the contract says.

    Otherwise what went wrong: an accepted call (the defect proper), another
    exception type, or a message that lacks the class, an offending key, the
    wording of a fixed-RN class (``takes no name``) or — for a named class —
    one of its naming props.
    """
    try:
        result = call()
    except TypeError as error:
        message = str(error)
        lacking = [part for part in (cls.__name__, *keys) if part not in message]
        naming = _naming(cls)
        if not naming:
            lacking += [part for part in ("takes no name",) if part not in message]
        else:
            lacking += [
                prop for prop in naming if not re.search(rf"\b{re.escape(prop)}\b", message)
            ]
        return f"TypeError message {message!r} lacks {lacking}" if lacking else None
    except Exception as error:  # any other outcome is the finding
        return f"raised {type(error).__name__}: {str(error)[:100]}"
    dn = getattr(result, "dn", result)
    return f"accepted silently (→ {dn!r})"


def _report(problems: list[str], total: int, what: str) -> str:
    # The silent acceptances (the defect proper) first, then the rest.
    ordered = sorted(problems, key=lambda problem: "silently" not in problem)
    shown = "\n".join(ordered[:15])
    return f"{len(problems)}/{total} {what} break the contract, e.g.:\n{shown}"


class _QueryProbe:
    """Stands in for ``node.query(cls)`` — the empty call needs no session."""

    def __init__(self, cls: type[ManagedObject]) -> None:
        self.cls = cls


@pytest.fixture
def probed_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``query()`` on both node flavours return a :class:`_QueryProbe`."""

    def query(_self: object, cls: type[ManagedObject]) -> _QueryProbe:
        return _QueryProbe(cls)

    monkeypatch.setattr(NiwakiNode, "query", query)
    monkeypatch.setattr(AsyncNiwakiNode, "query", query)


# ── 0. The sweeps are well-founded and not thinned out ────────────────────────


def test_the_sweeps_cover_the_whole_model_and_edge_universe() -> None:
    """Every family the rule touches is present, in bulk."""
    classes = _all_classes()
    assert len(classes) > 2_000, "the class sweep was thinned out"
    assert len(_fixed_classes()) > 900
    assert len(_named_classes()) > 1_200
    named = [_model(name) for name in _named_classes()]
    assert sum(len(_naming(cls)) > 1 for cls in named) > 100, "multi-naming-prop classes"
    assert sum(any(_alias(cls, p) for p in _naming(cls)) for cls in named) > 500, "wire aliases"
    assert (
        sum(any(not cls.model_fields[p].is_required() for p in _naming(cls)) for cls in named) > 150
    ), "optional naming props"
    assert len(_edges_of("kwargs")) > 7_000, "the kwargs-edge sweep was thinned out"
    assert len(_edges_of("name")) > 1_000, "the name-edge sweep was thinned out"
    assert len(_edges_of("fixed")) > 1_000


def test_naming_spellings_are_unambiguous() -> None:
    """The Python name and wire alias of a naming prop belong to no other field.

    The premise of "accept either spelling": were a non-naming field answering
    to a naming prop's alias, the same key would mean two things.
    """
    clashes: list[str] = []
    for name in _named_classes():
        cls = _model(name)
        owners: dict[str, set[str]] = {}
        for field_name in cls.model_fields:
            owners.setdefault(field_name, set()).add(field_name)
            if (alias := _alias(cls, field_name)) is not None:
                owners.setdefault(alias, set()).add(field_name)
        for prop in _naming(cls):
            for spelling in {prop, _alias(cls, prop) or prop}:
                if owners[spelling] != {prop}:
                    clashes.append(f"{name}.{prop}: {spelling!r} also names {owners[spelling]}")
    assert clashes == []


def test_valid_naming_values_derive_for_every_named_class() -> None:
    """The nominal and mixed sweeps reach every named class — none is skipped."""
    missing = [name for name in _named_classes() if _valid_naming(name) is None]
    assert missing == []


# ── 1. The reported defects ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["name", "tnQosCustomPolName", "target_name", "annotation", _BOGUS])
def test_mo_refuses_a_name_on_an_rs_singleton(client: Client, key: str) -> None:
    """``epg.mo(fvRsCustQosPol, name="gold")`` — the relation has a fixed RN, no name exists."""
    epg = client.tenant("prod").app("shop").epg("web")
    with pytest.raises(TypeError) as caught:
        epg.mo(fvRsCustQosPol, **{key: "gold"})
    message = str(caught.value)
    assert "takes no name" in message
    assert "fvRsCustQosPol" in message
    assert key in message


def test_mo_on_an_rs_singleton_without_argument_is_unchanged(client: Client) -> None:
    node = client.tenant("prod").app("shop").epg("web").mo(fvRsCustQosPol)
    assert node.dn == f"{_EPG}/rscustQosPol"
    assert node.cls is fvRsCustQosPol


def test_a_refused_mo_never_reaches_delete(client: Client) -> None:
    """The danger itself: ``…mo(fvRsCustQosPol, name="gold").delete()`` fails at ``mo()``.

    The client never connected: had the name been swallowed, ``delete()`` would
    have run and failed differently (no session) — only the refusal raises
    :class:`TypeError`.
    """
    epg = client.tenant("prod").app("shop").epg("web")
    with pytest.raises(TypeError, match="takes no name"):
        epg.mo(fvRsCustQosPol, name="gold").delete()


@pytest.mark.parametrize("how", ["edge", "mo"])
def test_a_subnet_scope_is_refused(client: Client, how: str) -> None:
    """``bd.subnet(subnet=..., scope="public")``: ``scope`` is configuration, not a name."""
    bd = client.tenant("prod").bd("web")
    call: Callable[[], object] = (
        (lambda: _edge_of(bd, "subnet")(subnet="10.0.0.1/24", scope="public"))
        if how == "edge"
        else (lambda: bd.mo(fvSubnet, subnet="10.0.0.1/24", scope="public"))
    )
    with pytest.raises(TypeError) as caught:
        call()
    message = str(caught.value)
    assert "fvSubnet" in message
    assert "scope" in message
    assert re.search(r"\bsubnet\b", message), "the naming prop is listed"


@pytest.mark.parametrize("how", ["edge", "mo"])
def test_a_port_config_with_only_a_description_is_refused(client: Client, how: str) -> None:
    """``fabric_port_port(description="x")`` used to fall back to an all-defaults port config."""
    fabric = client.fabric()
    call: Callable[[], object] = (
        (lambda: _edge_of(fabric, "fabric_port_port")(description="x"))
        if how == "edge"
        else (lambda: fabric.mo(fabricPortConfig, description="x"))
    )
    with pytest.raises(TypeError) as caught:
        call()
    message = str(caught.value)
    assert "fabricPortConfig" in message
    assert "description" in message
    for prop in ("node", "card", "port", "sub_port"):
        assert re.search(rf"\b{prop}\b", message), f"naming prop {prop!r} is not listed"


@pytest.mark.parametrize("how", ["edge", "mo"])
def test_a_port_config_with_a_description_beside_a_naming_prop_is_refused(
    client: Client, how: str
) -> None:
    """A *subset* of the naming props is valid on its own — the foreign key still is not."""
    fabric = client.fabric()
    call: Callable[[], object] = (
        (lambda: _edge_of(fabric, "fabric_port_port")(node=101, description="x"))
        if how == "edge"
        else (lambda: fabric.mo(fabricPortConfig, node=101, description="x"))
    )
    with pytest.raises(TypeError, match="description"):
        call()


def test_every_offending_key_is_named(client: Client) -> None:
    bd = client.tenant("prod").bd("web")
    with pytest.raises(TypeError) as caught:
        bd.mo(fvSubnet, subnet="10.0.0.1/24", scope="public", virtual=True)
    message = str(caught.value)
    assert "scope" in message
    assert "virtual" in message


def test_the_key_check_precedes_the_value_validation(client: Client) -> None:
    """A foreign key is a :class:`TypeError` whatever its (here invalid) value."""
    bd = client.tenant("prod").bd("web")
    with pytest.raises(TypeError):
        bd.mo(fvSubnet, subnet="10.0.0.1/24", scope="not-a-scope")
    with pytest.raises(TypeError):
        _edge_of(bd, "subnet")(subnet="10.0.0.1/24", scope="not-a-scope")
    with pytest.raises(TypeError):
        bd.mo(fvSubnet, scope="public")  # required naming prop missing *and* a foreign key


def test_a_typo_is_a_type_error_not_a_validation_error(client: Client) -> None:
    """``brandNewProp`` used to raise the model's :class:`~pydantic.ValidationError`."""
    tenant = client.tenant("prod")
    with pytest.raises(TypeError, match="brandNewProp"):
        tenant.mo(fvBD, name="web", brandNewProp="x")
    with pytest.raises(TypeError, match="brandNewProp"):
        _edge_of(tenant, "bd")(name="web", brandNewProp="x")


@pytest.mark.parametrize("key", ["name", "description", "children", "ctrl", "subnet_control"])
def test_a_real_field_that_is_not_a_naming_prop_is_refused(client: Client, key: str) -> None:
    """``name`` is a plain property of a subnet (its naming prop is ``subnet``)."""
    value: object = [] if key == "children" else "x"
    with pytest.raises(TypeError, match=key):
        client.tenant("prod").bd("web").mo(fvSubnet, subnet="10.0.0.1/24", **{key: value})


def test_a_positional_argument_to_mo_is_refused(client: Client) -> None:
    """``mo(cls, "web")`` — ``mo`` takes the class alone positionally."""
    mo = _edge_of(client.tenant("prod"), "mo")
    with pytest.raises(TypeError):
        mo(fvBD, "web")


def test_the_async_refusal_is_synchronous() -> None:
    """The async node's ``mo()`` is a plain function: the refusal needs no ``await``."""
    assert not inspect.iscoroutinefunction(AsyncNiwakiNode.mo)
    node = _node("fvBD", "async")
    with pytest.raises(TypeError):
        node.mo(fvSubnet, subnet="10.0.0.1/24", scope="public")
    with pytest.raises(TypeError):
        _edge_of(node, "subnet")(subnet="10.0.0.1/24", scope="public")


@pytest.mark.parametrize("edge", ["vmm_provider", "infra"])
def test_the_client_root_proxy_refuses_a_foreign_key(client: Client, edge: str) -> None:
    """``aci.vmm_provider(description="x")`` — the proxy delegates to the root node."""
    navigator = _edge_of(client, edge)
    with pytest.raises(TypeError):
        navigator(description="x")
    with pytest.raises(TypeError):
        navigator(vendor="VMware", description="x")


# ── 2. Nominal paths are unchanged ────────────────────────────────────────────


def test_a_subnet_by_python_name_and_by_wire_alias(client: Client) -> None:
    bd = client.tenant("prod").bd("web")
    expected = f"{_BD}/subnet-[10.0.0.1/24]"
    assert bd.subnet(subnet="10.0.0.1/24").dn == expected
    assert _dn(_edge_of(bd, "subnet")(ip="10.0.0.1/24")) == expected
    assert bd.mo(fvSubnet, subnet="10.0.0.1/24").dn == expected
    assert bd.mo(fvSubnet, ip="10.0.0.1/24").dn == expected


def test_mo_by_name_is_unchanged(client: Client) -> None:
    tenant = client.tenant("prod")
    node = tenant.mo(fvBD, name="web")
    assert (node.dn, node.cls) == (_BD, fvBD)


def test_a_multi_naming_prop_class_takes_either_spelling_in_any_mix(client: Client) -> None:
    profile = client.infra().leaf_profile("leaf101")
    expected = "uni/infra/nprof-leaf101/leaves-s1-typ-range"
    assert profile.leaf_selector(name="s1", selector_type="range").dn == expected
    assert _dn(_edge_of(profile, "leaf_selector")(name="s1", type="range")) == expected
    assert profile.mo(infraLeafS, name="s1", selector_type="range").dn == expected
    assert profile.mo(infraLeafS, type="range", name="s1").dn == expected


@pytest.mark.parametrize(
    ("keywords", "expected_rn"),
    [
        ({"node": 101}, "portconfnode-101-card-1-port-1-sub-0"),
        ({"node": 101, "card": 2}, "portconfnode-101-card-2-port-1-sub-0"),
        ({"node": 101, "sub_port": 3}, "portconfnode-101-card-1-port-1-sub-3"),
        ({"node": 101, "subPort": 3}, "portconfnode-101-card-1-port-1-sub-3"),
    ],
    ids=["node", "node+card", "node+sub_port", "node+wire-subPort"],
)
def test_optional_naming_props_are_still_accepted(
    client: Client, keywords: dict[str, object], expected_rn: str
) -> None:
    """All four naming props of a port config have defaults: any subset (≥ 1) names one."""
    fabric = client.fabric()
    assert _dn(_edge_of(fabric, "fabric_port_port")(**keywords)) == f"uni/fabric/{expected_rn}"
    assert fabric.mo(fabricPortConfig, **keywords).dn == f"uni/fabric/{expected_rn}"


def test_an_invalid_naming_value_is_still_a_validation_error(client: Client) -> None:
    """Refusing foreign *keys* does not take validation of the naming *values* away."""
    bd = client.tenant("prod").bd("web")
    with pytest.raises(ValidationError):
        bd.mo(fvSubnet, subnet="not an address!")
    with pytest.raises(ValidationError):
        _edge_of(bd, "subnet")(subnet="not an address!")
    with pytest.raises(ValidationError):
        client.tenant("prod").mo(fvBD, name="")


@pytest.mark.usefixtures("probed_query")
def test_a_keyword_edge_called_empty_is_still_a_query(client: Client) -> None:
    bd = client.tenant("prod").bd("web")
    for edge, child in (("subnet", fvSubnet), ("fabric_port_port", fabricPortConfig)):
        owner = bd if edge == "subnet" else client.fabric()
        result = _edge_of(owner, edge)()
        assert isinstance(result, _QueryProbe)
        assert result.cls is child


# ── 3. Every generated class through mo(), both flavours ──────────────────────


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_mo_refuses_every_non_naming_key_of_every_class(flavour_: Flavour) -> None:
    """Each non-naming field (by name and wire alias) and a typo, one key per call.

    A fixed-RN class refuses with ``takes no name``; a named class with its
    naming props listed.  The values are valid defaults, so the refusal can
    only be about the key.
    """
    node = _node("_root", flavour_)
    problems: list[str] = []
    total = 0
    for aci_class in _all_classes():
        cls = _model(aci_class)
        for key, value in _foreign_probes(cls):
            total += 1
            call = partial(_mo, node, cls, {key: value})
            if problem := _refusal_problem(call, cls, (key,)):
                problems.append(f"{aci_class}({key}=…): {problem}")
    assert total > 25_000, "the key sweep was thinned out"
    assert not problems, _report(problems, total, "mo() calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_mo_refuses_a_foreign_key_next_to_valid_naming_props(flavour_: Flavour) -> None:
    """The naming props are right, one key beside them is not — refused, not dropped."""
    node = _node("_root", flavour_)
    problems: list[str] = []
    total = 0
    for aci_class in _named_classes():
        cls = _model(aci_class)
        for wire in (False, True):
            naming = _naming_kwargs(cls, wire=wire)
            for key, value in _few_foreign_probes(cls):
                total += 1
                call = partial(_mo, node, cls, {**naming, key: value})
                if problem := _refusal_problem(call, cls, (key,)):
                    problems.append(f"{aci_class}({', '.join(naming)}, {key}=…): {problem}")
    assert total > 8_000, "the mixed sweep was thinned out"
    assert not problems, _report(problems, total, "mo() calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_mo_reaches_every_fixed_rn_class_without_argument(flavour_: Flavour) -> None:
    """``mo(cls)`` → ``<parent dn>/<the fixed RN>``, carrying the class."""
    node = _node("_root", flavour_)
    problems: list[str] = []
    for aci_class in _fixed_classes():
        cls = _model(aci_class)
        result = _mo(node, cls, {})
        got = (getattr(result, "dn", None), getattr(result, "cls", None))
        if got != (f"uni/{_rn_format(cls)}", cls):
            problems.append(f"{aci_class}: got {got}")
    assert not problems, _report(problems, len(_fixed_classes()), "fixed-RN classes")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_mo_with_naming_props_only_is_unchanged_for_every_named_class(flavour_: Flavour) -> None:
    """Python names and wire aliases reach the very same DN — the model's own RN."""
    node = _node("_root", flavour_)
    problems: list[str] = []
    for aci_class in _named_classes():
        cls = _model(aci_class)
        expected = (f"uni/{_expected_rn(cls)}", cls)
        for wire in (False, True):
            result = _mo(node, cls, _naming_kwargs(cls, wire=wire))
            got = (getattr(result, "dn", None), getattr(result, "cls", None))
            if got != expected:
                problems.append(f"{aci_class} (wire={wire}): got {got}, expected {expected}")
    assert not problems, _report(problems, 2 * len(_named_classes()), "named classes")


# ── 4. Every keyword navigation edge, both flavours ───────────────────────────


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_refuses_a_non_naming_key(flavour_: Flavour) -> None:
    """The typo, ``children``, one real field and one aliased field, alone in the call."""
    edges = _edges_of("kwargs")
    problems: list[str] = []
    total = 0
    for parent, edge, child in edges:
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        for key, value in _few_foreign_probes(cls):
            total += 1
            if problem := _refusal_problem(partial(navigator, **{key: value}), cls, (key,)):
                problems.append(f"{parent}.{edge}({key}=…) → {child}: {problem}")
    assert not problems, _report(problems, total, "keyword-edge calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_refuses_a_foreign_key_next_to_valid_naming_props(
    flavour_: Flavour,
) -> None:
    edges = _edges_of("kwargs")
    problems: list[str] = []
    total = 0
    for parent, edge, child in edges:
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        naming = _naming_kwargs(cls)
        for key, value in _few_foreign_probes(cls)[:2]:
            total += 1
            call = partial(navigator, **{**naming, key: value})
            if problem := _refusal_problem(call, cls, (key,)):
                problems.append(
                    f"{parent}.{edge}({', '.join(naming)}, {key}=…) → {child}: {problem}"
                )
    assert not problems, _report(problems, total, "keyword-edge calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_with_naming_props_only_is_unchanged(flavour_: Flavour) -> None:
    """Python names and wire aliases reach ``<parent dn>/<the model's RN>``."""
    edges = _edges_of("kwargs")
    problems: list[str] = []
    for parent, edge, child in edges:
        cls = _model(child)
        expected = (f"{_parent_dn(parent)}/{_expected_rn(cls)}", cls)
        navigator = _navigator(parent, edge, flavour_)
        for wire in (False, True):
            result = navigator(**_naming_kwargs(cls, wire=wire))
            got = (getattr(result, "dn", None), getattr(result, "cls", None))
            if got != expected:
                problems.append(f"{parent}.{edge} (wire={wire}) → {child}: got {got}")
    assert not problems, _report(problems, 2 * len(edges), "keyword edges")


@pytest.mark.usefixtures("probed_query")
@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_called_empty_is_still_a_query(flavour_: Flavour) -> None:
    edges = _edges_of("kwargs")
    problems: list[str] = []
    for parent, edge, child in edges:
        try:
            result = _navigator(parent, edge, flavour_)()
        except Exception as error:  # any raise is the finding
            problems.append(f"{parent}.{edge} → {child}: raised {error!r}")
            continue
        if not (isinstance(result, _QueryProbe) and result.cls is _model(child)):
            problems.append(f"{parent}.{edge} → {child}: got {result!r}")
    assert not problems, _report(problems, len(edges), "keyword edges")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_name_edge_refuses_a_non_naming_key(flavour_: Flavour) -> None:
    """A ``name`` edge's signature is ``(name="")``: any other keyword is a :class:`TypeError`.

    Already true (Python's own refusal) — pinned so the ``name`` edges keep it
    whatever the refusal of the other two shapes ends up looking like.
    """
    edges = _edges_of("name")
    problems: list[str] = []
    for parent, edge, child in edges:
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        for key, value in _few_foreign_probes(cls):
            for keywords in ({key: value}, {"name": "gold", key: value}):
                try:
                    result = navigator(**keywords)
                except TypeError:
                    continue
                except Exception as error:  # any other outcome is the finding
                    problems.append(f"{parent}.{edge}({key}=…) → {child}: raised {error!r}")
                    continue
                problems.append(f"{parent}.{edge}({key}=…) → {child}: accepted → {result!r}")
    assert not problems, _report(problems, len(edges), "name edges")


# ── 5. Whose words, name edges, one spelling per naming prop ─────────────────

_PRIVATE_NAMES = ("_nav_name", "_nav_kwargs", "<locals>")
_SENTINEL = "s3cr3t-sentinel-value"


def _attribution_problem(
    call: Callable[[], object],
    cls: Model,
    keys: Sequence[str],
    *,
    edge: str | None,
    words: Sequence[str],
    phrases: Sequence[str] = (),
) -> str | None:
    """``None`` when *call* refuses with a message in the caller's own words.

    The subject is the door the call went through: ``mo(<Class>)`` for ``mo()``
    (``edge is None``), ``<edge>(`` for a navigation edge — which then never
    says ``mo(<Class>…``.  The message also names the class and every key of
    *keys*, contains each of *words* as a whole word and each of *phrases*
    verbatim, and leaks no private navigator name (``_nav_name``…).
    """
    try:
        result = call()
    except TypeError as error:
        message = str(error)
    except Exception as error:  # any other outcome is the finding
        return f"raised {type(error).__name__}: {str(error)[:100]}"
    else:
        return f"accepted silently (→ {_dn(result)!r})"
    name = cls.__name__
    subject = f"mo({name})" if edge is None else f"{edge}("
    lacking = [part for part in (name, subject, *keys, *phrases) if part not in message]
    lacking += [
        word for word in words if not re.search(rf"(?<!\w){re.escape(word)}(?!\w)", message)
    ]
    problems: list[str] = []
    if lacking:
        problems.append(f"lacks {lacking}")
    if edge is not None and f"mo({name}" in message:
        problems.append(f"names mo({name}…) although the call went through {edge}(…)")
    if leaked := [private for private in _PRIVATE_NAMES if private in message]:
        problems.append(f"leaks the private name(s) {leaked}")
    return f"TypeError message {message!r} " + "; ".join(problems) if problems else None


def _foreign_expectations(cls: Model) -> tuple[list[str], tuple[str, ...]]:
    """What a foreign-key refusal must say: the naming props, or that nothing can be named."""
    naming = _naming(cls)
    return (naming, ()) if naming else ([], ("takes no name",))


@cache
def _aliased_naming() -> tuple[tuple[str, str, str], ...]:
    """Every ``(class, naming prop, wire alias)`` whose wire name differs from its Python name."""
    return tuple(
        (name, prop, alias)
        for name in _named_classes()
        for prop in _naming(_model(name))
        if (alias := _alias(_model(name), prop)) is not None
    )


def _duplicated_spelling(
    cls: Model, prop: str, alias: str, *, alias_first: bool
) -> dict[str, object]:
    """Valid naming keywords where *prop* is given twice: by Python name and by wire alias."""
    naming = _naming_kwargs(cls)
    if alias_first:
        return {alias: naming[prop], **naming}
    return {**naming, alias: naming[prop]}


def test_the_round_two_sweeps_are_not_thinned_out() -> None:
    assert len(_aliased_naming()) > 600, "naming props with a distinct wire alias"
    aliased_classes = {name for name, _prop, _alias_ in _aliased_naming()}
    kwargs_edges = [edge for edge in _edges_of("kwargs") if edge[2] in aliased_classes]
    assert len(kwargs_edges) > 500, "keyword edges to a class with an aliased naming prop"
    assert len(_edges_of("name")) > 1_000


# ── 5a. The name edges refuse an extra keyword in their own words ────────────


@pytest.mark.parametrize(
    ("args", "keywords"),
    [
        (("web",), {"arp_flooding": True}),
        ((), {"name": "web", "arp_flooding": True}),
        ((), {"arp_flooding": True}),
        (("web",), {"arp_flooding": True, "description": "x"}),
        ((), {"arp_flooding": True, "description": "x", _BOGUS: 1}),
    ],
    ids=["positional+kw", "name-kw+kw", "kw-alone", "two-keys", "three-keys"],
)
def test_a_name_edge_refuses_an_extra_keyword_in_its_own_words(
    client: Client, args: tuple[object, ...], keywords: dict[str, object]
) -> None:
    """``tenant.bd("web", arp_flooding=True)`` — not ``_nav_name() got an unexpected keyword``."""
    call = partial(_edge_of(client.tenant("prod"), "bd"), *args, **keywords)
    offending = tuple(key for key in keywords if key != "name")
    problem = _attribution_problem(call, fvBD, offending, edge="bd", words=["name"])
    assert problem is None, problem


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_name_edge_refuses_an_extra_keyword_in_its_own_words(flavour_: Flavour) -> None:
    """Each edge, four probe keys, three call shapes, plus a call with two foreign keys."""
    edges = _edges_of("name")
    problems: list[str] = []
    total = 0
    for parent, edge, child in edges:
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        probes = _few_foreign_probes(cls)
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        for key, value in probes:
            calls += [
                ((), {key: value}),
                (("gold",), {key: value}),
                ((), {"name": "gold", key: value}),
            ]
        calls.append(((), dict(probes[:2])))
        for args, keywords in calls:
            total += 1
            offending = tuple(key for key in keywords if key != "name")
            call = partial(navigator, *args, **keywords)
            if problem := _attribution_problem(call, cls, offending, edge=edge, words=["name"]):
                problems.append(f"{parent}.{edge}({', '.join(keywords)}) → {child}: {problem}")
    assert total > 14_000, "the name-edge message sweep was thinned out"
    assert not problems, _report(problems, total, "name-edge calls")


# ── 5b. One spelling per naming prop ──────────────────────────────────────────


@pytest.mark.parametrize("alias_first", [False, True], ids=["python-first", "alias-first"])
@pytest.mark.parametrize("values", ["same", "different"])
@pytest.mark.parametrize("how", ["edge", "mo"])
def test_both_spellings_of_a_naming_prop_are_refused(
    client: Client, how: str, values: str, alias_first: bool
) -> None:
    """``bd.mo(fvSubnet, subnet=…, ip=…)`` used to address the ``ip`` value, silently."""
    bd = client.tenant("prod").bd("web")
    second = "10.0.0.1/24" if values == "same" else "10.9.9.9/24"
    keywords: dict[str, object] = {"subnet": "10.0.0.1/24", "ip": second}
    if alias_first:
        keywords = dict(reversed(keywords.items()))
    if how == "edge":
        call = partial(_edge_of(bd, "subnet"), **keywords)
    else:
        call = partial(_edge_of(bd, "mo"), fvSubnet, **keywords)
    problem = _attribution_problem(
        call, fvSubnet, (), edge="subnet" if how == "edge" else None, words=["subnet", "ip"]
    )
    assert problem is None, problem


@pytest.mark.parametrize("how", ["edge", "mo"])
def test_a_duplicated_spelling_beside_other_naming_props_is_refused(
    client: Client, how: str
) -> None:
    """``infraLeafS``: ``selector_type`` and ``type`` are one prop; ``name`` is another."""
    profile = client.infra().leaf_profile("leaf101")
    keywords: dict[str, object] = {"name": "s1", "selector_type": "range", "type": "range"}
    if how == "edge":
        call = partial(_edge_of(profile, "leaf_selector"), **keywords)
    else:
        call = partial(_edge_of(profile, "mo"), infraLeafS, **keywords)
    problem = _attribution_problem(
        call,
        infraLeafS,
        (),
        edge="leaf_selector" if how == "edge" else None,
        words=["selector_type", "type"],
    )
    assert problem is None, problem


@pytest.mark.parametrize("how", ["edge", "mo"])
def test_the_duplicate_refusal_echoes_no_value(client: Client, how: str) -> None:
    bd = client.tenant("prod").bd("web")
    keywords: dict[str, object] = {"subnet": "10.1.2.3/24", "ip": "10.4.5.6/24"}
    call = (
        partial(_edge_of(bd, "subnet"), **keywords)
        if how == "edge"
        else partial(_edge_of(bd, "mo"), fvSubnet, **keywords)
    )
    with pytest.raises(TypeError) as caught:
        call()
    message = str(caught.value)
    assert "10.1.2.3" not in message
    assert "10.4.5.6" not in message


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_mo_refuses_both_spellings_of_every_aliased_naming_prop(flavour_: Flavour) -> None:
    """All naming props with a distinct wire alias, both orders, the other naming props valid."""
    node = _node("_root", flavour_)
    problems: list[str] = []
    total = 0
    for aci_class, prop, alias in _aliased_naming():
        cls = _model(aci_class)
        for alias_first in (False, True):
            total += 1
            keywords = _duplicated_spelling(cls, prop, alias, alias_first=alias_first)
            call = partial(_mo, node, cls, keywords)
            if problem := _attribution_problem(call, cls, (), edge=None, words=[prop, alias]):
                problems.append(f"{aci_class}({', '.join(keywords)}): {problem}")
    assert total > 1_200, "the duplicate-spelling sweep was thinned out"
    assert not problems, _report(problems, total, "mo() calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_refuses_both_spellings_of_every_aliased_naming_prop(
    flavour_: Flavour,
) -> None:
    aliased: dict[str, list[tuple[str, str]]] = {}
    for aci_class, prop, alias in _aliased_naming():
        aliased.setdefault(aci_class, []).append((prop, alias))
    problems: list[str] = []
    total = 0
    for parent, edge, child in _edges_of("kwargs"):
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        for prop, alias in aliased.get(child, ()):
            for alias_first in (False, True):
                total += 1
                keywords = _duplicated_spelling(cls, prop, alias, alias_first=alias_first)
                call = partial(navigator, **keywords)
                if problem := _attribution_problem(call, cls, (), edge=edge, words=[prop, alias]):
                    problems.append(f"{parent}.{edge}({', '.join(keywords)}) → {child}: {problem}")
    assert total > 1_400, "the duplicate-spelling edge sweep was thinned out"
    assert not problems, _report(problems, total, "keyword-edge calls")


# ── 5c. A refusal speaks the language of the door it came through ────────────

type Owner = Callable[[Client], object]

_EDGE_REFUSALS: list[tuple[str, Owner, str, dict[str, object], str, str]] = [
    (
        "subnet",
        lambda c: c.tenant("prod").bd("web"),
        "subnet",
        {"subnet": "10.0.0.1/24", "scope": "public"},
        "fvSubnet",
        "scope",
    ),
    (
        "port-config",
        lambda c: c.fabric(),
        "fabric_port_port",
        {"node": 101, "description": "x"},
        "fabricPortConfig",
        "description",
    ),
    (
        "root-proxy",
        lambda c: c,
        "vmm_provider",
        {"vendor": "VMware", "description": "x"},
        "vmmProvP",
        "description",
    ),
    (
        "duplicate",
        lambda c: c.tenant("prod").bd("web"),
        "subnet",
        {"subnet": "10.0.0.1/24", "ip": "10.0.0.1/24"},
        "fvSubnet",
        "ip",
    ),
]


@pytest.mark.parametrize(
    ("owner", "edge", "keywords", "child", "key"),
    [case[1:] for case in _EDGE_REFUSALS],
    ids=[case[0] for case in _EDGE_REFUSALS],
)
def test_a_refusal_through_an_edge_names_the_edge_as_called(
    client: Client, owner: Owner, edge: str, keywords: dict[str, object], child: str, key: str
) -> None:
    """``subnet(...) takes naming properties only…`` — never ``mo(fvSubnet)``."""
    call = partial(_edge_of(owner(client), edge), **keywords)
    problem = _attribution_problem(call, _model(child), (key,), edge=edge, words=[])
    assert problem is None, problem


_MO_REFUSALS: list[tuple[str, Owner, type[ManagedObject], dict[str, object], str]] = [
    (
        "subnet",
        lambda c: c.tenant("prod").bd("web"),
        fvSubnet,
        {"subnet": "10.0.0.1/24", "scope": "public"},
        "scope",
    ),
    (
        "port-config",
        lambda c: c.fabric(),
        fabricPortConfig,
        {"node": 101, "description": "x"},
        "description",
    ),
    (
        "rs-singleton",
        lambda c: c.tenant("prod").app("shop").epg("web"),
        fvRsCustQosPol,
        {"name": "gold"},
        "name",
    ),
    (
        "duplicate",
        lambda c: c.tenant("prod").bd("web"),
        fvSubnet,
        {"subnet": "10.0.0.1/24", "ip": "10.0.0.1/24"},
        "ip",
    ),
]


@pytest.mark.parametrize(
    ("owner", "cls", "keywords", "key"),
    [case[1:] for case in _MO_REFUSALS],
    ids=[case[0] for case in _MO_REFUSALS],
)
def test_a_refusal_through_mo_names_mo_and_the_class(
    client: Client, owner: Owner, cls: type[ManagedObject], keywords: dict[str, object], key: str
) -> None:
    call = partial(_edge_of(owner(client), "mo"), cls, **keywords)
    problem = _attribution_problem(call, cls, (key,), edge=None, words=[])
    assert problem is None, problem


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_kwargs_edge_refusal_names_the_edge_as_called(flavour_: Flavour) -> None:
    """Each keyword edge, each probe key — alone, then beside the valid naming props."""
    problems: list[str] = []
    total = 0
    for parent, edge, child in _edges_of("kwargs"):
        cls = _model(child)
        navigator = _navigator(parent, edge, flavour_)
        words, phrases = _foreign_expectations(cls)
        naming = _naming_kwargs(cls)
        for key, value in _few_foreign_probes(cls):
            for keywords in ({key: value}, {**naming, key: value}):
                total += 1
                call = partial(navigator, **keywords)
                if problem := _attribution_problem(
                    call, cls, (key,), edge=edge, words=words, phrases=phrases
                ):
                    problems.append(f"{parent}.{edge}({', '.join(keywords)}) → {child}: {problem}")
    assert total > 45_000, "the edge-message sweep was thinned out"
    assert not problems, _report(problems, total, "keyword-edge calls")


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_mo_refusal_names_mo_and_the_class(flavour_: Flavour) -> None:
    """Every class, each probe key alone (and beside the valid naming props of a named class)."""
    node = _node("_root", flavour_)
    problems: list[str] = []
    total = 0
    for aci_class in _all_classes():
        cls = _model(aci_class)
        words, phrases = _foreign_expectations(cls)
        naming = _naming_kwargs(cls) if _naming(cls) else {}
        for key, value in _few_foreign_probes(cls):
            for keywords in ({key: value}, {**naming, key: value}):
                total += 1
                call = partial(_mo, node, cls, keywords)
                if problem := _attribution_problem(
                    call, cls, (key,), edge=None, words=words, phrases=phrases
                ):
                    problems.append(f"{aci_class}({', '.join(keywords)}): {problem}")
    assert total > 15_000, "the mo() message sweep was thinned out"
    assert not problems, _report(problems, total, "mo() calls")


# ── 5d. Keys only: a refusal never echoes a value ────────────────────────────


@pytest.mark.parametrize(
    ("owner", "edge", "keywords"),
    [
        (
            lambda c: c.tenant("prod").bd("web"),
            "mo",
            {"cls": fvSubnet, "subnet": "10.0.0.1/24", "scope": _SENTINEL},
        ),
        (
            lambda c: c.tenant("prod").bd("web"),
            "subnet",
            {"subnet": "10.0.0.1/24", "scope": _SENTINEL},
        ),
        (lambda c: c.tenant("prod"), "bd", {"name": "web", "description": _SENTINEL}),
        (
            lambda c: c.tenant("prod").app("shop").epg("web"),
            "mo",
            {"cls": fvRsCustQosPol, "name": _SENTINEL},
        ),
    ],
    ids=["mo", "kwargs-edge", "name-edge", "fixed-rn-mo"],
)
def test_a_refusal_echoes_keys_never_values(
    client: Client, owner: Owner, edge: str, keywords: dict[str, object]
) -> None:
    arguments = dict(keywords)
    leading = (arguments.pop("cls"),) if "cls" in arguments else ()
    with pytest.raises(TypeError) as caught:
        _edge_of(owner(client), edge)(*leading, **arguments)
    assert _SENTINEL not in str(caught.value)
