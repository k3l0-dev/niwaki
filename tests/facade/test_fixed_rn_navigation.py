"""A fixed-RN navigation edge takes no name — the facade refuses one, loudly.

The defect this module pins: ``epg.custom_qos_policy("gold")`` was accepted
and the name silently dropped.  The child (``fvRsCustQosPol``) is an Rs
singleton whose RN is fixed (``rscustQosPol``, no naming placeholder), so
whatever name is passed the node points at the same DN — and
``epg.custom_qos_policy("gold").delete()`` deleted the EPG's QoS *relation*
while the caller believed they deleted ``"gold"``.

The rule (sync and async identical — the nodes are twins):

* **Which edges** — a navigation edge is *fixed-RN* when its child model has
  no naming property (``child._naming_props == []``).  That is exactly the
  set whose RN format carries no ``{placeholder}`` (measured: no exception
  across ``CHILD_MAP``).  It covers the Rs singletons that carry a target-name
  property (``RS_TARGET_PROP``: ``fvRsCustQosPol``, ``fvRsCtx``,
  ``fvRsNdPfxPol``…) **and** every other fixed-RN child (``pimBDP``,
  ``infraInfra``, ``fvAccP``…).
* **Refusal** — calling a fixed-RN edge with *any* argument, positional or
  keyword (``"gold"``, ``""``, ``name="gold"``, ``target_name="gold"``),
  raises :class:`TypeError` at the call, before any request.  ``TypeError``
  because it is an unexpected argument (the facade reports no "missing name"
  error to align with: a named edge called without a name yields a
  ``Query``).  The message names the edge, says it takes no name, and shows
  the correct call — it contains, verbatim, the edge name, the words
  ``takes no name`` and ``<edge>()``.
* **No argument** — ``<edge>()`` keeps working and yields the very DN it
  yields today: ``<parent dn>/<the child's fixed RN>``, carrying the child
  class.
* **Signature** — the runtime navigator's :func:`inspect.signature` is
  ``()``: the stubs mirror the runtime shape (``tests/typing/
  test_nav_conformance.py``), so a refusing navigator must still *say* it
  takes nothing (``*args, **kwargs`` plus a hand-written refusal would read
  as a different shape there; a ``__signature__`` restores it).
* **Named edges are untouched** — a ``name`` edge still takes its name
  positionally or as ``name=``; a ``kwargs`` edge still takes its naming
  props; called empty, both still yield a ``Query``.

The sweeps cover every fixed-RN edge of ``CHILD_MAP`` (≈1 400 edges, ≈250
of them Rs singletons; both flavours; a few seconds) — navigation is local,
so nodes are built without a session.  Names a node API shadows are unreachable and skipped (as the
conformance test does).  The static side (passing a name is a type error) is
pinned in ``tests/typing/test_nav_static.py``; the design DSL's own refusal in
``tests/design/test_fixed_rn_makers.py``.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterator
from functools import cache
from typing import TYPE_CHECKING, Literal, cast

import pytest

from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki._codegen.generate_domain import _node_api_names, _root_api_names
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, RS_TARGET_PROP
from niwaki.models._generated.fv.fvAccP import fvAccP
from niwaki.models._generated.fv.fvRsCtx import fvRsCtx
from niwaki.models._generated.fv.fvRsCustQosPol import fvRsCustQosPol
from niwaki.models._generated.fv.fvRsNdPfxPol import fvRsNdPfxPol
from niwaki.models._generated.infra.infraInfra import infraInfra
from niwaki.models._generated.pim.pimBDP import pimBDP

if TYPE_CHECKING:
    from niwaki.models.base import ManagedObject

type Flavour = Literal["sync", "async"]
type Client = Niwaki | AsyncNiwaki
type AnyNode = NiwakiNode[ManagedObject] | AsyncNiwakiNode[ManagedObject]

FLAVOURS: tuple[Flavour, ...] = ("sync", "async")

# Every way of passing a name the old navigators accepted (or Python refused
# with a message that named nothing): positional, empty positional, the two
# keywords.  Each must be refused.
NAME_CALLS: dict[str, tuple[tuple[object, ...], dict[str, object]]] = {
    "positional": (("gold",), {}),
    "empty-positional": (("",), {}),
    "name-keyword": ((), {"name": "gold"}),
    "target-name-keyword": ((), {"target_name": "gold"}),
}

_EPG = "uni/tn-prod/ap-shop/epg-web"


# ── Helpers ───────────────────────────────────────────────────────────────────


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


@cache
def _model(aci_class: str) -> type[ManagedObject]:
    module = importlib.import_module(f"niwaki.models._generated.{CLASS_PKG[aci_class]}.{aci_class}")
    return cast("type[ManagedObject]", getattr(module, aci_class))


def _parent_dn(parent: str) -> str:
    return "uni" if parent == "_root" else "uni/x"


def _node(parent: str, flavour: Flavour) -> AnyNode:
    """A session-less node of *parent*'s class (the root row is ``polUni``'s)."""
    cls = _model("polUni" if parent == "_root" else parent)
    dn = _parent_dn(parent)
    if flavour == "sync":
        return NiwakiNode(cast("Niwaki", None), dn, cls)
    return AsyncNiwakiNode(cast("AsyncNiwaki", None), dn, cls)


def _reserved(parent: str) -> frozenset[str]:
    """Names a node (or, at the root, the client) API shadows — unreachable edges."""
    return _root_api_names() | _node_api_names() if parent == "_root" else _node_api_names()


def _edges(*, fixed: bool) -> Iterator[tuple[str, str, str]]:
    """Every reachable ``(parent, edge, child)`` whose child is (not) fixed-RN, sorted."""
    for parent in sorted(CHILD_MAP):
        if parent != "_root" and parent not in CLASS_PKG:
            continue
        reserved = _reserved(parent)
        for edge, child in sorted(CHILD_MAP[parent].items()):
            if edge in reserved or child not in CLASS_PKG:
                continue
            if (not _model(child)._naming_props) is fixed:  # pyright: ignore[reportPrivateUsage]
                yield parent, edge, child


@cache
def _fixed_edges() -> tuple[tuple[str, str, str], ...]:
    return tuple(_edges(fixed=True))


@cache
def _named_edges() -> tuple[tuple[str, str, str], ...]:
    return tuple(_edges(fixed=False))


def _navigator(parent: str, edge: str, flavour: Flavour) -> Callable[..., object]:
    navigator: object = getattr(_node(parent, flavour), edge)
    assert callable(navigator)
    return navigator


def _refusal_problem(
    call: Callable[..., object], edge: str, args: tuple[object, ...], kwargs: dict[str, object]
) -> str | None:
    """``None`` when *call* refuses the name as the contract says, else what went wrong."""
    try:
        result = call(*args, **kwargs)
    except TypeError as error:
        message = str(error)
        missing = [part for part in (edge, "takes no name", f"{edge}()") if part not in message]
        if missing:
            return f"TypeError message {message!r} lacks {missing}"
        return None
    except Exception as error:  # any other outcome is the finding
        return f"raised {type(error).__name__}: {error}"
    dn = getattr(result, "dn", result)
    return f"accepted the name silently (→ {dn!r})"


def _report(problems: list[str], total: int) -> str:
    # The silent acceptances (the defect proper) first, then the rest.
    ordered = sorted(problems, key=lambda problem: "silently" not in problem)
    shown = "\n".join(ordered[:15])
    return f"{len(problems)}/{total} fixed-RN edges break the contract, e.g.:\n{shown}"


# ── 0. The rule is well-founded ───────────────────────────────────────────────


def test_fixed_rn_means_no_naming_prop_and_no_placeholder() -> None:
    """``_naming_props == []`` ⟺ the RN format carries no placeholder — one rule."""
    mismatched = [
        (parent, edge, child)
        for parent, row in CHILD_MAP.items()
        for edge, child in row.items()
        if child in CLASS_PKG
        and (not _model(child)._naming_props)  # pyright: ignore[reportPrivateUsage]
        != ("{" not in _model(child)._rn_format)  # pyright: ignore[reportPrivateUsage]
    ]
    assert mismatched == []


def test_the_sweep_covers_both_families() -> None:
    """The fixed-RN set holds Rs singletons with a target prop and plain singletons."""
    fixed = _fixed_edges()
    rs = [edge for edge in fixed if edge[2] in RS_TARGET_PROP]
    plain = [edge for edge in fixed if edge[2] not in RS_TARGET_PROP]
    assert len(fixed) > 1_000, "the fixed-RN sweep was thinned out"
    assert len(rs) > 100
    assert len(plain) > 500
    assert ("fvAEPg", "custom_qos_policy", "fvRsCustQosPol") in fixed
    assert ("fvBD", "pim", "pimBDP") in fixed
    assert ("_root", "infra", "infraInfra") in fixed


# ── 1. The reported defect: custom_qos_policy("gold") ─────────────────────────


@pytest.mark.parametrize(("args", "kwargs"), NAME_CALLS.values(), ids=NAME_CALLS.keys())
def test_custom_qos_policy_refuses_a_name(
    client: Client, args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    epg: object = client.tenant("prod").app("shop").epg("web")
    navigator = getattr(epg, "custom_qos_policy")  # noqa: B009 — a static error when typed
    with pytest.raises(TypeError) as caught:
        navigator(*args, **kwargs)
    message = str(caught.value)
    assert "custom_qos_policy" in message
    assert "takes no name" in message
    assert "custom_qos_policy()" in message


def test_custom_qos_policy_delete_never_reaches_the_relation(client: Client) -> None:
    """The danger itself: ``…custom_qos_policy("gold").delete()`` fails at the call.

    The client never connected: had the name been swallowed, ``delete()``
    would have run and failed differently (no session) — only the refusal
    raises :class:`TypeError`.
    """
    epg: object = client.tenant("prod").app("shop").epg("web")
    navigator = getattr(epg, "custom_qos_policy")  # noqa: B009 — a static error when typed
    with pytest.raises(TypeError, match="takes no name"):
        navigator("gold").delete()


def test_custom_qos_policy_without_argument_is_unchanged(client: Client) -> None:
    node = client.tenant("prod").app("shop").epg("web").custom_qos_policy()
    assert node.dn == f"{_EPG}/rscustQosPol"
    assert node.cls is fvRsCustQosPol


# ── 2. Representative edges: Rs singletons and plain singletons ───────────────


def _bd(client: Client) -> object:
    return client.tenant("prod").bd("web")


def _subnet(client: Client) -> object:
    return client.tenant("prod").bd("web").subnet(subnet="10.0.0.1/24")


# (label, parent getter, edge, expected DN of the empty call, child class)
REPRESENTATIVES: list[tuple[str, Callable[[Client], object], str, str, type[ManagedObject]]] = [
    ("rs-vrf-binding", _bd, "vrf_binding", "uni/tn-prod/BD-web/rsctx", fvRsCtx),
    (
        "rs-nd-prefix-policy",
        _subnet,
        "nd_prefix_policy",
        "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]/rsNdPfxPol",
        fvRsNdPfxPol,
    ),
    ("plain-pim", _bd, "pim", "uni/tn-prod/BD-web/pimbdp", pimBDP),
    ("plain-legacy-mode", _bd, "legacy_mode", "uni/tn-prod/BD-web/accp", fvAccP),
    ("plain-root-infra", lambda client: client.root, "infra", "uni/infra", infraInfra),
]


@pytest.mark.parametrize(
    ("parent", "edge", "expected_dn", "child"),
    [case[1:] for case in REPRESENTATIVES],
    ids=[case[0] for case in REPRESENTATIVES],
)
@pytest.mark.parametrize(("args", "kwargs"), NAME_CALLS.values(), ids=NAME_CALLS.keys())
def test_representative_fixed_rn_edge_refuses_a_name(
    client: Client,
    parent: Callable[[Client], object],
    edge: str,
    expected_dn: str,
    child: type[ManagedObject],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    del expected_dn, child
    navigator = getattr(parent(client), edge)
    assert _refusal_problem(navigator, edge, args, kwargs) is None


@pytest.mark.parametrize(
    ("parent", "edge", "expected_dn", "child"),
    [case[1:] for case in REPRESENTATIVES],
    ids=[case[0] for case in REPRESENTATIVES],
)
def test_representative_fixed_rn_edge_without_argument_is_unchanged(
    client: Client,
    parent: Callable[[Client], object],
    edge: str,
    expected_dn: str,
    child: type[ManagedObject],
) -> None:
    node = getattr(parent(client), edge)()
    assert (node.dn, node.cls) == (expected_dn, child)


@pytest.mark.parametrize(("args", "kwargs"), NAME_CALLS.values(), ids=NAME_CALLS.keys())
def test_client_root_proxy_refuses_a_name(
    client: Client, args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    """``aci.infra("x")`` — the client's root proxy refuses like the root node."""
    navigator = getattr(client, "infra")  # noqa: B009 — a static error when typed
    assert _refusal_problem(navigator, "infra", args, kwargs) is None


# ── 3. Every fixed-RN edge, both flavours ─────────────────────────────────────


@pytest.mark.parametrize("flavour_", FLAVOURS)
@pytest.mark.parametrize(("args", "kwargs"), NAME_CALLS.values(), ids=NAME_CALLS.keys())
def test_every_fixed_rn_edge_refuses_a_name(
    flavour_: Flavour, args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    edges = _fixed_edges()
    problems = [
        f"{parent}.{edge} → {child}: {problem}"
        for parent, edge, child in edges
        if (problem := _refusal_problem(_navigator(parent, edge, flavour_), edge, args, kwargs))
    ]
    assert not problems, _report(problems, len(edges))


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_fixed_rn_edge_without_argument_is_unchanged(flavour_: Flavour) -> None:
    """``<edge>()`` → ``<parent dn>/<fixed RN>``, carrying the child class."""
    edges = _fixed_edges()
    problems: list[str] = []
    for parent, edge, child in edges:
        cls = _model(child)
        expected = f"{_parent_dn(parent)}/{cls._rn_format}"  # pyright: ignore[reportPrivateUsage]
        node = _navigator(parent, edge, flavour_)()
        got = (getattr(node, "dn", None), getattr(node, "cls", None))
        if got != (expected, cls):
            problems.append(f"{parent}.{edge} → {child}: got {got}, expected {(expected, cls)}")
    assert not problems, _report(problems, len(edges))


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_fixed_rn_navigator_declares_no_parameter(flavour_: Flavour) -> None:
    """The runtime shape the stubs mirror: ``inspect.signature`` reads ``()``."""
    edges = _fixed_edges()
    problems = [
        f"{parent}.{edge} → {child}: {signature}"
        for parent, edge, child in edges
        if (signature := inspect.signature(_navigator(parent, edge, flavour_))).parameters
    ]
    assert not problems, _report(problems, len(edges))


# ── 4. Named edges are untouched (regression guard) ──────────────────────────


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


@pytest.mark.usefixtures("probed_query")
@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_named_edge_called_empty_is_still_a_query(flavour_: Flavour) -> None:
    edges = _named_edges()
    assert len(edges) > 5_000, "the named-edge sweep was thinned out"
    problems: list[str] = []
    for parent, edge, child in edges:
        try:
            result = _navigator(parent, edge, flavour_)()
        except Exception as error:  # any raise is the finding
            problems.append(f"{parent}.{edge} → {child}: raised {error!r}")
            continue
        if not (isinstance(result, _QueryProbe) and result.cls is _model(child)):
            problems.append(f"{parent}.{edge} → {child}: got {result!r}")
    assert not problems, f"{len(problems)} named edges changed, e.g.:\n" + "\n".join(problems[:15])


@pytest.mark.parametrize("flavour_", FLAVOURS)
def test_every_name_edge_still_takes_its_name(flavour_: Flavour) -> None:
    """A ``name`` edge: positional and ``name=`` reach ``<parent>/<rn with the name>``."""
    edges = [
        edge
        for edge in _named_edges()
        if _model(edge[2])._naming_props == ["name"]  # pyright: ignore[reportPrivateUsage]
    ]
    assert len(edges) > 1_000, "the name-edge sweep was thinned out"
    problems: list[str] = []
    for parent, edge, child in edges:
        cls = _model(child)
        rn = cls._rn_format.replace("{name}", "gold")  # pyright: ignore[reportPrivateUsage]
        expected = (f"{_parent_dn(parent)}/{rn}", cls)
        navigator = _navigator(parent, edge, flavour_)
        for got in (navigator("gold"), navigator(name="gold")):
            if (getattr(got, "dn", None), getattr(got, "cls", None)) != expected:
                problems.append(f"{parent}.{edge} → {child}: got {got!r}, expected {expected}")
    assert not problems, f"{len(problems)} name edges changed, e.g.:\n" + "\n".join(problems[:15])


@pytest.mark.parametrize(
    ("call", "expected_dn"),
    [
        (lambda c: c.tenant("prod"), "uni/tn-prod"),
        (lambda c: c.tenant(name="prod").bd("web"), "uni/tn-prod/BD-web"),
        (
            lambda c: c.tenant("prod").bd("web").subnet(subnet="10.0.0.1/24"),
            "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]",
        ),
        (
            lambda c: c.tenant("prod").custom_qos_policy("gold"),
            "uni/tn-prod/qoscustom-gold",
        ),
        (
            lambda c: (
                c.infra().leaf_profile("leaf101").leaf_selector(name="s1", selector_type="range")
            ),
            "uni/infra/nprof-leaf101/leaves-s1-typ-range",
        ),
        (lambda c: c.vmm_provider(vendor="VMware").vmm_dom("dc1"), "uni/vmmp-VMware/dom-dc1"),
        (
            lambda c: c.fabric().fabric_port_port(node=101),
            "uni/fabric/portconfnode-101-card-1-port-1-sub-0",
        ),
    ],
    ids=[
        "tenant",
        "bd-by-keyword",
        "subnet-kwargs",
        "tenant-qos-policy-is-named",
        "leaf-selector-mixed-kwargs",
        "vmm-optional-kwargs",
        "fabric-port-all-optional",
    ],
)
def test_named_edges_sample_is_unchanged(
    client: Client, call: Callable[[Client], object], expected_dn: str
) -> None:
    """Hand-picked named edges, including the *named* ``custom_qos_policy`` under a tenant."""
    assert getattr(call(client), "dn", None) == expected_dn
