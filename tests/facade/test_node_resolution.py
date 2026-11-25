"""``node(dn)`` resolves its class from the DN — the root vocabulary lives at ``uni`` only.

The defect this module pins (the ``_root`` aliasing): a node built without a
class used to carry :class:`~niwaki.models.base.ManagedObject`, and the
navigation table keys the top level under ``"_root"`` *for that very class* —
so ``aci.node("uni/tn-x").tenant("y")`` silently built the impossible DN
``uni/tn-x/tn-y``, while a node genuinely typed ``polUni`` had no navigation
at all.

The contract (sync and async identical — every test runs both flavours):

* **Resolution** — ``node(dn)`` without a class walks the DN from ``uni``
  (RN segment by RN segment, against the containment tables) and carries the
  class it lands on: ``node("uni")`` is ``polUni`` and offers the root
  vocabulary; ``node("uni/tn-x")`` is ``fvTenant`` and offers the tenant
  vocabulary (``.bd("web")``), never the root's (``.tenant("y")`` raises
  :class:`AttributeError` naming the class).  ``aci.root`` is the
  ``polUni`` node too.
* **Unresolvable DNs** (an unknown segment, a DN outside ``uni``) and DNs
  that land on a class with no generated model (``faultInst``) still yield a
  node — :meth:`read`, :meth:`query` and
  :meth:`delete` need the DN only, and keep working — whose class is
  :class:`~niwaki.models.base.ManagedObject` and whose navigation raises a
  clear :class:`AttributeError` naming the DN.  **Never** the root vocabulary.
  :meth:`mo` with an explicit class still descends from such a node.
* **Explicit class** — ``node(dn, cls)`` keeps *cls*.  A class the DN
  contradicts is refused with :class:`ValueError` at the call (fail-loud):
  when the DN resolves, *cls* must be the resolved class; when it does not,
  *cls*'s RN format must at least parse the DN's last RN segment.
* **Breaking (2.2)** — the root vocabulary is no longer offered at an
  arbitrary DN; :func:`test_root_vocabulary_is_gone_from_arbitrary_dns` is
  the explicit pin.
* **``__dir__``** on the clients and on every node lists exactly the
  navigation names valid there (plus the ordinary attributes), so IPython and
  Jupyter complete them; deprecated aliases resolve but are never offered.
* **No side effects** — resolution is local (no request, see the ``aci``
  fixture's strict HTTP mock), and never imports the design DSL.
* **Runtime shape unchanged** — nodes are still plain
  :class:`~niwaki.NiwakiNode`/:class:`~niwaki.AsyncNiwakiNode` with their
  pre-2.2 MRO (typed navigation is a checker-only fiction).

Resolver: the walk is the one ``to_design()`` already uses for scoped
imports (``design/_import.py``: ``_resolve_child_class`` + the DN-grammar
fallback ``_grammar_children`` — CHILD_MAP, then the generated ``_contains``,
then the catalogue's ``dn_formats``; measured: zero RN collisions inside any
containment table).  It must be factored into a shared private module both
callers use — the facade may not import the design DSL (pinned by
:func:`test_node_resolution_never_imports_the_design_dsl`).  Where the walk
raises (no match, or the theoretical ambiguous segment), ``node()`` treats the
DN as unresolvable; it never guesses.

The static side (``node(dn, cls)``/``mo(cls)`` typed) is pinned in
``tests/typing/test_node_static.py``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from typing import Literal

import pytest
from pytest_httpx import HTTPXMock

from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki.domain._child_map import CHILD_MAP
from niwaki.models._generated.fv.fvAccP import fvAccP
from niwaki.models._generated.fv.fvAEPg import fvAEPg
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvCtx import fvCtx
from niwaki.models._generated.fv.fvRsCtx import fvRsCtx
from niwaki.models._generated.fv.fvSubnet import fvSubnet
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models._generated.infra.infraInfra import infraInfra
from niwaki.models._generated.infra.infraLeafS import infraLeafS
from niwaki.models._generated.l2ext.l2extLIfP import l2extLIfP
from niwaki.models._generated.l3ext.l3extLIfP import l3extLIfP
from niwaki.models._generated.pol.polUni import polUni
from niwaki.models._generated.top.topRoot import topRoot
from niwaki.models._generated.vmm.vmmDomP import vmmDomP
from niwaki.models.base import ManagedObject
from tests.conftest import HOST, LOGIN_URL, load_fixture, login_payload, ok

type Flavour = Literal["sync", "async"]
type Client = Niwaki | AsyncNiwaki
type AnyNode = NiwakiNode[ManagedObject] | AsyncNiwakiNode[ManagedObject]

FLAVOURS: tuple[Flavour, ...] = ("sync", "async")

# DNs no walk from uni can resolve: an unknown segment (at the top level and
# under a tenant), a DN outside uni.
UNRESOLVABLE = (
    "uni/tn-x/nope-1",
    "uni/tn-x/nope-1/BD-web",
    "uni/zzz-1",
    "topology/pod-1/node-101/sys",
)

# Lands on a class the catalogue knows but no generated model backs.
NOT_GENERATED = "uni/tn-x/fault-F0123"


def _client(flavour: Flavour) -> Client:
    """A client that never connects: navigation is local, no session needed."""
    if flavour == "sync":
        return Niwaki("https://apic.invalid", "admin", "secret")
    return AsyncNiwaki("https://apic.invalid", "admin", "secret")


def _node_type(flavour: Flavour) -> type[object]:
    return NiwakiNode if flavour == "sync" else AsyncNiwakiNode


def _attribute(obj: object, name: str) -> object:
    """Dynamic attribute access — some probed names are static errors on typed nodes."""
    return getattr(obj, name)


def _untyped(client: Client, dn: str) -> AnyNode:
    """``node(dn)`` without a class (statically ``NiwakiNode[ManagedObject]``)."""
    return client.node(dn)


@pytest.fixture(params=FLAVOURS)
def flavour(request: pytest.FixtureRequest) -> Flavour:
    value: Flavour = request.param
    return value


@pytest.fixture
def client(flavour: Flavour) -> Client:
    return _client(flavour)


# ── 1. Resolution from the DN ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dn", "cls"),
    [
        ("uni", polUni),
        ("uni/tn-x", fvTenant),
        ("uni/tn-common", fvTenant),
        ("uni/tn-x/BD-web", fvBD),
        ("uni/tn-x/ctx-prod", fvCtx),
        ("uni/tn-x/BD-web/rsctx", fvRsCtx),
        ("uni/tn-x/BD-web/accp", fvAccP),
        ("uni/tn-x/BD-web/subnet-[10.0.0.1/24]", fvSubnet),
        ("uni/tn-x/ap-a/epg-e", fvAEPg),
        ("uni/infra", infraInfra),
        ("uni/infra/nprof-leaf101/leaves-s1-typ-range", infraLeafS),
        ("uni/vmmp-VMware/dom-dc1", vmmDomP),
    ],
)
def test_node_without_class_resolves_it_from_the_dn(
    client: Client, flavour: Flavour, dn: str, cls: type[ManagedObject]
) -> None:
    """Every RN segment is walked from ``uni``: brackets, multi-prop RNs, Rs singletons."""
    node = _untyped(client, dn)
    assert type(node) is _node_type(flavour)
    assert node.dn == dn
    assert node.cls is cls


def test_root_is_the_policy_universe(client: Client) -> None:
    """``aci.root`` carries ``polUni`` — the one class whose vocabulary is the root's."""
    assert client.root.cls is polUni
    assert client.root.dn == "uni"


def test_uni_offers_the_root_vocabulary(client: Client) -> None:
    for root in (_untyped(client, "uni"), client.root, client.node("uni", polUni)):
        tenant = root.tenant("prod")
        assert (tenant.dn, tenant.cls) == ("uni/tn-prod", fvTenant)
        assert root.infra().dn == "uni/infra"
        assert root.vmm_provider(vendor="VMware").vmm_dom("dc1").dn == "uni/vmmp-VMware/dom-dc1"


def test_client_proxies_the_root_vocabulary_unchanged(client: Client) -> None:
    bd = client.tenant("prod").bd("web")
    assert (bd.dn, bd.cls) == ("uni/tn-prod/BD-web", fvBD)


def test_resolved_node_offers_its_own_class_vocabulary(client: Client) -> None:
    tenant = _untyped(client, "uni/tn-x")
    bd = tenant.bd("web")
    assert (bd.dn, bd.cls) == ("uni/tn-x/BD-web", fvBD)
    subnet = _untyped(client, "uni/tn-x/BD-web").subnet(subnet="10.0.0.1/24")
    assert (subnet.dn, subnet.cls) == ("uni/tn-x/BD-web/subnet-[10.0.0.1/24]", fvSubnet)
    epg = _untyped(client, "uni/tn-x/ap-a").epg("web")
    assert (epg.dn, epg.cls) == ("uni/tn-x/ap-a/epg-web", fvAEPg)
    profile = _untyped(client, "uni/infra").leaf_profile("leaf101")
    assert profile.dn == "uni/infra/nprof-leaf101"


def test_resolved_node_navigation_matches_typed_navigation(client: Client) -> None:
    """Resolving the class is the same as stating it: same child DN, same class."""
    resolved = _untyped(client, "uni/tn-x/BD-web").vrf_binding()
    stated = client.node("uni/tn-x/BD-web", fvBD).vrf_binding()
    walked = client.tenant("x").bd("web").vrf_binding()
    assert resolved.dn == stated.dn == walked.dn == "uni/tn-x/BD-web/rsctx"
    assert resolved.cls is stated.cls is walked.cls is fvRsCtx


# ── 2. The breaking change: no root vocabulary at an arbitrary DN ─────────────


@pytest.mark.parametrize(
    ("dn", "cls_name", "attr"),
    [
        ("uni/tn-x", "fvTenant", "tenant"),
        ("uni/tn-x", "fvTenant", "infra"),
        ("uni/tn-x", "fvTenant", "vmm_provider"),
        ("uni/tn-x/BD-web", "fvBD", "tenant"),
        ("uni/infra", "infraInfra", "fabric"),
    ],
)
def test_root_vocabulary_is_gone_from_arbitrary_dns(
    client: Client, dn: str, cls_name: str, attr: str
) -> None:
    """Breaking in 2.2: ``aci.node("uni/tn-x").tenant("y")`` no longer builds ``uni/tn-x/tn-y``."""
    node = _untyped(client, dn)
    with pytest.raises(AttributeError, match=rf"{cls_name}.*{attr}|{attr}.*{cls_name}"):
        getattr(node, attr)
    assert not hasattr(node, attr)


def test_typed_node_has_no_root_vocabulary_either(client: Client) -> None:
    node = client.node("uni/tn-x", fvTenant)
    with pytest.raises(AttributeError, match="fvTenant"):
        _attribute(node, "tenant")


# ── 3. Unresolvable DNs and classes without a model ───────────────────────────


@pytest.mark.parametrize("dn", [*UNRESOLVABLE, NOT_GENERATED])
def test_node_without_class_never_raises(client: Client, dn: str) -> None:
    """``read()``/``delete()`` need the DN only: building the node always succeeds."""
    node = _untyped(client, dn)
    assert node.dn == dn
    assert node.cls is ManagedObject


@pytest.mark.parametrize("dn", [*UNRESOLVABLE, NOT_GENERATED])
@pytest.mark.parametrize("attr", ["tenant", "bd", "infra", "tag", "annotation"])
def test_unresolved_node_has_no_navigation(client: Client, dn: str, attr: str) -> None:
    """Never the root vocabulary — nor the universal edges — on an unknown class."""
    node = _untyped(client, dn)
    with pytest.raises(AttributeError) as caught:
        getattr(node, attr)
    message = str(caught.value)
    assert attr in message
    assert dn in message, f"the error must name the DN it could not resolve: {message}"
    assert not hasattr(node, attr)


def test_unresolved_node_still_descends_with_an_explicit_class(client: Client) -> None:
    """``mo(cls)`` states the class itself: the child navigates by that class."""
    child = _untyped(client, "uni/tn-x/nope-1").mo(fvBD, name="web")
    assert (child.dn, child.cls) == ("uni/tn-x/nope-1/BD-web", fvBD)
    assert child.subnet(subnet="10.0.0.1/24").cls is fvSubnet


def test_private_names_still_raise_plain_attribute_error(client: Client) -> None:
    """Dunder/private probes (copy, pickle, IPython) never hit the resolver."""
    for dn in ("uni/tn-x", UNRESOLVABLE[0]):
        node = _untyped(client, dn)
        with pytest.raises(AttributeError):
            _attribute(node, "_repr_html_")
        with pytest.raises(AttributeError):
            _attribute(node, "__wrapped__")


class TestUnresolvedNodeTerminalsSync:
    """``read``/``query``/``delete`` on an unresolvable DN keep working (sync)."""

    DN = "topology/pod-1/node-101/sys"

    def test_read(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/{self.DN}.json",
            json={
                "totalCount": "1",
                "imdata": [{"topSystem": {"attributes": {"dn": self.DN, "name": "leaf101"}}}],
            },
        )
        mo = aci.node(self.DN).read()
        assert mo.dn == self.DN
        assert mo["name"] == "leaf101"

    def test_query(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=ok())
        assert aci.node(self.DN).query("l1PhysIf").fetch() == []
        get = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert get[0].url.path == f"/api/mo/{self.DN}.json"

    def test_delete(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="DELETE", json=ok())
        aci.node(NOT_GENERATED).delete()
        deletes = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
        assert deletes[0].url.path == f"/api/mo/{NOT_GENERATED}.json"


class TestUnresolvedNodeTerminalsAsync:
    """Same contract on the async facade."""

    DN = "topology/pod-1/node-101/sys"

    async def test_read(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/{self.DN}.json",
            json={
                "totalCount": "1",
                "imdata": [{"topSystem": {"attributes": {"dn": self.DN, "name": "leaf101"}}}],
            },
        )
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            mo = await aci.node(self.DN).read()
        assert mo.dn == self.DN

    async def test_query_and_delete(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=ok())
        httpx_mock.add_response(method="DELETE", json=ok())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert await aci.node(self.DN).query("l1PhysIf").fetch() == []
            await aci.node(NOT_GENERATED).delete()
        paths = {(r.method, r.url.path) for r in httpx_mock.get_requests()}
        assert ("GET", f"/api/mo/{self.DN}.json") in paths
        assert ("DELETE", f"/api/mo/{NOT_GENERATED}.json") in paths


def test_resolved_node_reads_its_concrete_model(aci: Niwaki, httpx_mock: HTTPXMock) -> None:
    """The resolved class deserialises the read: a ``fvBD``, not a bare ``ManagedObject``."""
    httpx_mock.add_response(
        method="GET",
        url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
        json=load_fixture("fvBD_list"),
    )
    bd = aci.node("uni/tn-Prod/BD-Prod-BD").read()
    assert isinstance(bd, fvBD)
    assert bd.name == "Prod-BD"


async def test_resolved_async_node_reads_its_concrete_model(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    httpx_mock.add_response(
        method="GET",
        url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
        json=load_fixture("fvBD_list"),
    )
    async with AsyncNiwaki(HOST, "admin", "secret") as aci:
        bd = await aci.node("uni/tn-Prod/BD-Prod-BD").read()
    assert isinstance(bd, fvBD)


# ── 4. Explicit class ─────────────────────────────────────────────────────────


def test_explicit_class_matching_the_dn_is_kept(client: Client) -> None:
    node = client.node("uni/tn-x/BD-web", fvBD)
    assert node.cls is fvBD
    assert node.subnet(subnet="10.0.0.1/24").dn == "uni/tn-x/BD-web/subnet-[10.0.0.1/24]"
    assert client.node("uni", polUni).cls is polUni


@pytest.mark.parametrize(
    ("dn", "cls", "resolved"),
    [
        ("uni/tn-x/BD-web", fvTenant, "fvBD"),
        ("uni/tn-x", fvBD, "fvTenant"),
        ("uni", fvTenant, "polUni"),
        ("uni/tn-x", polUni, "fvTenant"),
        (NOT_GENERATED, fvBD, "faultInst"),
    ],
)
def test_explicit_class_contradicting_the_dn_fails_loud(
    client: Client, dn: str, cls: type[ManagedObject], resolved: str
) -> None:
    """Refused at the call — naming both the stated and the resolved class."""
    with pytest.raises(ValueError) as caught:
        client.node(dn, cls)
    message = str(caught.value)
    assert cls.__name__ in message
    assert resolved in message
    assert dn in message


def test_explicit_class_on_an_unresolvable_dn_is_trusted_when_its_rn_fits(
    client: Client,
) -> None:
    """The containment tables can miss an edge: a fitting class is the caller's word."""
    node = client.node("uni/tn-x/nope-1/BD-web", fvBD)
    assert node.cls is fvBD
    assert node.subnet(subnet="10.0.0.1/24").cls is fvSubnet


@pytest.mark.parametrize(
    ("dn", "cls"),
    [
        ("uni/tn-x/nope-1/BD-web", fvTenant),
        ("topology/pod-1/node-101/sys", fvBD),
    ],
)
def test_explicit_class_whose_rn_cannot_fit_an_unresolvable_dn_fails_loud(
    client: Client, dn: str, cls: type[ManagedObject]
) -> None:
    with pytest.raises(ValueError, match=re.escape(cls.__name__)):
        client.node(dn, cls)


# ── 5. __dir__ for IPython / Jupyter completion ───────────────────────────────


def _listed_navigation(obj: object) -> set[str]:
    """What ``dir()`` adds on top of the type's own attributes (public names only)."""
    return {name for name in dir(obj) if not name.startswith("_")} - set(dir(type(obj)))


def test_client_dir_lists_the_root_vocabulary(client: Client) -> None:
    listed = set(dir(client))
    assert {"tenant", "infra", "fabric", "vmm_provider"} <= listed
    assert {"node", "query", "root"} <= listed  # ordinary attributes stay
    assert "bd" not in listed
    assert _listed_navigation(client) == set(CHILD_MAP["_root"])


@pytest.mark.parametrize(
    ("dn", "row", "cls"),
    [
        ("uni", "_root", polUni),
        ("uni/tn-x", "fvTenant", fvTenant),
        ("uni/tn-x/BD-web", "fvBD", fvBD),
    ],
)
def test_node_dir_lists_exactly_the_vocabulary_of_its_class(
    client: Client, dn: str, row: str, cls: type[ManagedObject]
) -> None:
    """Resolved or stated, a node lists its class's row — and only it."""
    for node in (_untyped(client, dn), client.node(dn, cls)):
        listed = set(dir(node))
        assert {"read", "query", "delete", "mo", "dn", "cls"} <= listed
        assert _listed_navigation(node) == set(CHILD_MAP[row])


def test_root_node_dir_lists_the_root_vocabulary(client: Client) -> None:
    assert _listed_navigation(client.root) == set(CHILD_MAP["_root"])
    assert "bd" not in dir(client.root)


@pytest.mark.parametrize("dn", [*UNRESOLVABLE, NOT_GENERATED])
def test_unresolved_node_dir_lists_no_navigation(client: Client, dn: str) -> None:
    node = _untyped(client, dn)
    assert {"read", "query", "delete", "mo", "dn", "cls"} <= set(dir(node))
    assert _listed_navigation(node) == set()


def test_every_listed_name_resolves(client: Client) -> None:
    """Completion never offers a name that ``getattr`` then refuses."""
    nodes: list[object] = [
        client,
        client.root,
        _untyped(client, "uni/tn-x"),
        client.tenant("x").bd("web"),
        client.infra().leaf_profile("leaf101"),
    ]
    for obj in nodes:
        names = _listed_navigation(obj)
        assert names
        for name in names:
            assert callable(getattr(obj, name)), f"{obj!r}.{name}"


def test_deprecated_aliases_resolve_but_are_not_offered(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    import niwaki.domain._child_map as cm

    monkeypatch.setitem(cm.NAV_DEPRECATED, "fvCtx", {"pim_ctx": "pimCtxP"})
    vrf = client.tenant("t").vrf("v")
    with pytest.warns(DeprecationWarning):
        assert callable(_attribute(vrf, "pim_ctx"))
    assert "pim_ctx" not in dir(vrf)
    assert "pim" in dir(vrf)


def test_dir_performs_no_io(aci: Niwaki) -> None:
    """``dir()`` is local: the strict HTTP mock would fail on any request."""
    dir(aci)
    dir(aci.node("uni/tn-x"))
    dir(aci.node(UNRESOLVABLE[0]))


# ── 6. Side effects and runtime shape ─────────────────────────────────────────


def test_node_resolution_performs_no_io(aci: Niwaki) -> None:
    """Resolution is local — the strict HTTP mock fails on any unexpected request."""
    assert aci.node("uni/tn-x/BD-web").cls is fvBD
    assert aci.node(UNRESOLVABLE[2]).cls is ManagedObject


def test_node_resolution_never_imports_the_design_dsl() -> None:
    """The facade observes; resolving a DN must not drag the design DSL in."""
    script = textwrap.dedent(
        """
        import sys
        from niwaki import AsyncNiwaki, Niwaki

        for client in (Niwaki("https://apic.invalid", "a", "b"),
                       AsyncNiwaki("https://apic.invalid", "a", "b")):
            assert client.node("uni/tn-x/BD-web").cls.__name__ == "fvBD"
            assert callable(client.node("uni/tn-x/ap-a/epg-e").subnet)
            client.node("uni/tn-x/nope-1")
            dir(client.node("uni/tn-x"))
        loaded = sorted(m for m in sys.modules if m.startswith("niwaki.design"))
        assert not loaded, loaded
        """
    )
    run = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_nodes_stay_plain_runtime_classes(flavour: Flavour) -> None:
    """Typed navigation is a checker fiction: same runtime class, same MRO as 2.1."""
    client = _client(flavour)
    expected = _node_type(flavour)
    for node in (
        client.root,
        client.node("uni/tn-x"),
        client.node("uni/tn-x", fvTenant),
        client.node(UNRESOLVABLE[0]),
        client.tenant("x").mo(fvBD, name="web"),
    ):
        assert type(node) is expected
    assert [c.__name__ for c in NiwakiNode.__mro__] == [
        "NiwakiNode",
        "_JargonNavMixin",
        "Generic",
        "object",
    ]
    assert [c.__name__ for c in AsyncNiwakiNode.__mro__] == [
        "AsyncNiwakiNode",
        "_JargonNavMixin",
        "Generic",
        "object",
    ]


# ── 7. Review round 2: why a class is unknown, and the empty parent DN ────────

# The walk finds several classes for the last segment.  A catalogue-only
# case (no candidate has a model) and a generated case (every candidate the
# user can state is a real model).
AMBIGUOUS_CATALOGUE = "uni/infra/nodecfgcont/node-101"
AMBIGUOUS_CATALOGUE_CANDIDATES = ("fabricNodeCfg", "infraNodeCfg")
AMBIGUOUS_GENERATED = "uni/tn-x/acIpToIp-y/acl3outcont/outdef-z/lnodep-n/lifp-i"
AMBIGUOUS_GENERATED_CANDIDATES = ("l2extLIfP", "l2extLIfPDef", "l3extLIfP", "l3extLIfPDef")

# Resolves to a class the catalogue knows but no generated model backs.
CATALOGUE_ONLY = "uni/userext/appuser-x"


def _navigation_error(node: object, attr: str = "tenant") -> str:
    with pytest.raises(AttributeError) as caught:
        _attribute(node, attr)
    return str(caught.value)


@pytest.mark.parametrize(
    ("dn", "candidates"),
    [
        (AMBIGUOUS_CATALOGUE, AMBIGUOUS_CATALOGUE_CANDIDATES),
        (AMBIGUOUS_GENERATED, AMBIGUOUS_GENERATED_CANDIDATES),
    ],
)
def test_ambiguous_dn_says_ambiguous_and_names_the_candidates(
    client: Client, dn: str, candidates: tuple[str, ...]
) -> None:
    """Ambiguity is never silent: the error names every candidate and the way out."""
    node = _untyped(client, dn)
    assert node.cls is ManagedObject  # the untyped node still reads/queries/deletes
    message = _navigation_error(node)
    assert "ambiguous" in message, message
    for candidate in candidates:
        assert candidate in message, f"{candidate} missing from: {message}"
    assert dn in message
    assert "node(" in message, f"the error must say to state the class: {message}"
    assert "unknown" not in message, f"an ambiguous DN is not an unknown class: {message}"
    assert _listed_navigation(node) == set()


@pytest.mark.parametrize("stated", [l3extLIfP, l2extLIfP])
def test_stating_one_candidate_of_an_ambiguous_dn_works(
    client: Client, stated: type[ManagedObject]
) -> None:
    node = client.node(AMBIGUOUS_GENERATED, stated)
    assert node.cls is stated
    assert node.dn == AMBIGUOUS_GENERATED
    row = CHILD_MAP[stated.__name__]
    assert _listed_navigation(node) == set(row)
    assert callable(_attribute(node, sorted(row)[0]))


def test_stating_a_class_that_cannot_name_an_ambiguous_dn_fails_loud(client: Client) -> None:
    with pytest.raises(ValueError, match="fvBD"):
        client.node(AMBIGUOUS_GENERATED, fvBD)


@pytest.mark.parametrize(
    ("dn", "resolved"), [(CATALOGUE_ONLY, "aaaAppUser"), (NOT_GENERATED, "faultInst")]
)
def test_catalogue_only_class_is_named_not_called_unknown(
    client: Client, dn: str, resolved: str
) -> None:
    """The DN resolves — to a class without a generated model; the error says which."""
    message = _navigation_error(_untyped(client, dn))
    assert resolved in message, message
    assert "class is unknown" not in message, message


def test_unresolvable_dn_error_does_not_claim_ambiguity(client: Client) -> None:
    message = _navigation_error(_untyped(client, "uni/tn-x/nope-1"))
    assert "ambiguous" not in message, message


def test_empty_parent_dn_joins_a_child_as_its_rn(client: Client) -> None:
    """``topRoot`` sits at DN ``""``: its children are ``uni``, not ``/uni``."""
    top = client.node("", topRoot)
    assert top.dn == ""
    universe = top.policy_universe()
    assert (universe.dn, universe.cls) == ("uni", polUni)
    assert universe.tenant("x").dn == "uni/tn-x"
    assert universe.tenant("x").bd("web").dn == "uni/tn-x/BD-web"
    assert top.mo(polUni).dn == "uni"
