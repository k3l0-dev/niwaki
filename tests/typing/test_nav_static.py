# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Typed facade navigation: the static contract, and proof the runtime is untouched.

Facade navigation (``aci.tenant("prod").bd("web").subnet(subnet=...)``) is
resolved at runtime by ``__getattr__`` against ``CHILD_MAP`` — invisible to a
type checker, which used to see ``Any`` all the way down.  Typed navigation
classes exist **for the type checker only**: they live in ``.pyi`` stubs under
``niwaki._generated_nav`` (a package with no ``.py`` module at all) and are
wired into :mod:`niwaki.facade` through ``TYPE_CHECKING`` hooks.

This module pins three things:

1. **Static side** — the ``_static_*`` functions are never called; they exist
   for the type checkers.  pyright reads this file in ``scripts/checks.sh
   --fast``; mypy reads it through :func:`test_mypy_accepts_static_contract`
   (the public CI runs mypy on ``src`` only).  The probes sample every domain
   and every navigation shape, sync and async:

   * ``name`` — ``Literal[""]``/no argument → ``Query``, a ``str`` → node
     (tenant → bd, tenant → app → epg, infra → leaf profile, fabric →
     datetime policy → NTP provider);
   * ``kwargs`` with a required naming prop — no argument → ``Query``,
     keyword-only Python names → node (bd → subnet, epg → static path,
     leaf profile → leaf selector, infra → VLAN pool, the root's NDO profile);
   * ``kwargs`` whose naming props are **all optional** — one overload per
     prop, that prop required, so the empty call still means ``Query``
     (root → VMM provider, datetime policy → NTP auth key, fabric → port
     config with four optional props);
   * singleton ``()`` (infra, fabric, bd → pim);
   * Rs-singleton ``(target_name="", **kwargs)`` (bd → vrf binding, subnet →
     ND prefix policy);
   * the three return families — a parent with specific edges gets its own
     typed class (``fvBDNode``), a class whose only edges are the universal
     ones gets ``NavLeaf[C]``, a class with no edge at all gets ``NavEnd[C]``;
   * ``aci.root`` and the root-level universal edges;
   * ``read()`` returning the concrete model.
2. **Expected errors** — typos and wrong arguments carry a scoped
   ``# type: ignore[code]`` plus an ``# expect: <pyright rules>`` marker.
   Each checker fails if an expected error disappears or changes:

   * mypy (strict ``warn_unused_ignores``) pins the exact error code;
   * pyright honours ``# type: ignore`` as a *blanket* suppression, so a
     second ``# pyright: ignore[rule]`` on the same line would itself be
     reported unnecessary.  The header comment switches on
     ``reportUnnecessaryTypeIgnoreComment`` (the error must still exist), and
     :func:`test_pyright_pins_expected_errors` re-runs pyright with type-ignore
     comments disabled to pin the exact rules against the markers.

   The errors pinned: a typo on a typed node (every family: parent, leaf,
   end, root node), ``aci.tenantt`` on the client, a **wire alias** passed as
   a naming kwarg (``subnet(ip=...)``, ``static_path(tDn=...)``,
   ``leaf_selector(type=...)``, ``vlan_pool(allocMode=...)``,
   ``ntp_auth_key(id=...)``, ``fabric_port_port(subPort=...)``,
   ``ndo_controller_profile(ip=..., name=...)``) — the runtime accepts wire
   aliases, the typed surface speaks Python names only, as the design DSL
   does — a positional argument to a kwargs edge, a missing required naming
   prop, an argument to a singleton, a wrongly-typed name.
3. **Runtime side** — the typed classes never exist at runtime: navigation
   still returns plain :class:`~niwaki.NiwakiNode`, :class:`~niwaki.Niwaki`
   still derives from :class:`object` only, and no name or submodule of
   ``niwaki._generated_nav`` resolves at runtime.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

import pytest

from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode

if TYPE_CHECKING:
    from niwaki._generated_nav import (
        AsyncNavEnd,
        AsyncNavLeaf,
        AsyncRootNode,
        NavEnd,
        NavLeaf,
        RootNode,
    )
    from niwaki._generated_nav.datetime import (
        AsyncdatetimeNtpProvNode,
        AsyncdatetimePolNode,
        datetimeNtpProvNode,
        datetimePolNode,
    )
    from niwaki._generated_nav.fabric import AsyncfabricInstNode, fabricInstNode
    from niwaki._generated_nav.fv import (
        AsyncfvAEPgNode,
        AsyncfvApNode,
        AsyncfvBDNode,
        AsyncfvRsCtxNode,
        AsyncfvRsPathAttNode,
        AsyncfvSubnetNode,
        AsyncfvTenantNode,
        fvAEPgNode,
        fvApNode,
        fvBDNode,
        fvRsCtxNode,
        fvRsPathAttNode,
        fvSubnetNode,
        fvTenantNode,
    )
    from niwaki._generated_nav.fvns import AsyncfvnsVlanInstPNode, fvnsVlanInstPNode
    from niwaki._generated_nav.infra import (
        AsyncinfraInfraNode,
        AsyncinfraLeafSNode,
        AsyncinfraNodePNode,
        infraInfraNode,
        infraLeafSNode,
        infraNodePNode,
    )
    from niwaki._generated_nav.pim import AsyncpimBDPNode, pimBDPNode
    from niwaki._generated_nav.vmm import (
        AsyncvmmDomPNode,
        AsyncvmmProvPNode,
        vmmDomPNode,
        vmmProvPNode,
    )
    from niwaki.models.datetime.datetimeNtpAuthKey import datetimeNtpAuthKey
    from niwaki.models.datetime.datetimeNtpProv import datetimeNtpProv
    from niwaki.models.datetime.datetimePol import datetimePol
    from niwaki.models.fabric.fabricInst import fabricInst
    from niwaki.models.fabric.fabricPortConfig import fabricPortConfig
    from niwaki.models.fv.fvAccP import fvAccP
    from niwaki.models.fv.fvAEPg import fvAEPg
    from niwaki.models.fv.fvAp import fvAp
    from niwaki.models.fv.fvBD import fvBD
    from niwaki.models.fv.fvRsCtx import fvRsCtx
    from niwaki.models.fv.fvRsNdPfxPol import fvRsNdPfxPol
    from niwaki.models.fv.fvRsPathAtt import fvRsPathAtt
    from niwaki.models.fv.fvSubnet import fvSubnet
    from niwaki.models.fv.fvTenant import fvTenant
    from niwaki.models.fvns.fvnsVlanInstP import fvnsVlanInstP
    from niwaki.models.infra.infraInfra import infraInfra
    from niwaki.models.infra.infraLeafS import infraLeafS
    from niwaki.models.infra.infraNodeBlk import infraNodeBlk
    from niwaki.models.infra.infraNodeP import infraNodeP
    from niwaki.models.pol.polNdoProfile import polNdoProfile
    from niwaki.models.tag.tagAnnotation import tagAnnotation
    from niwaki.models.tag.tagTag import tagTag
    from niwaki.models.vmm.vmmDomP import vmmDomP
    from niwaki.models.vmm.vmmProvP import vmmProvP
    from niwaki.query import AsyncQuery, Query

_REPO = Path(__file__).resolve().parents[2]

_TDN = "topology/pod-1/paths-101/pathep-[eth1/1]"


# ── 1. Static side, sync ──────────────────────────────────────────────────────


def _static_tenant_chain(aci: Niwaki, bd_name: str) -> None:
    """tenant → bd → subnet; tenant → app → epg → static path."""
    # name shape: "" / no argument → Query, a str → typed node.
    assert_type(aci.tenant("prod"), fvTenantNode)
    assert_type(aci.tenant(), Query[fvTenant])
    assert_type(aci.tenant(""), Query[fvTenant])
    assert_type(aci.tenant(name="prod"), fvTenantNode)
    assert_type(aci.tenant("prod").bd("web"), fvBDNode)
    assert_type(aci.tenant("prod").bd(bd_name), fvBDNode)
    assert_type(aci.tenant("prod").bd(), Query[fvBD])
    # kwargs shape, required naming prop under its Python name.
    bd = aci.tenant("prod").bd("web")
    assert_type(bd.subnet(subnet="10.0.0.1/24"), fvSubnetNode)
    assert_type(bd.subnet(), Query[fvSubnet])
    app = aci.tenant("prod").app("shop")
    assert_type(app, fvApNode)
    assert_type(app.epg("web"), fvAEPgNode)
    assert_type(app.epg(), Query[fvAEPg])
    assert_type(app.epg("web").static_path(target_dn=_TDN), fvRsPathAttNode)
    assert_type(app.epg("web").static_path(), Query[fvRsPathAtt])
    # A child reachable from two parents is the same typed class from both.
    assert_type(app.epg("web").subnet(subnet="10.0.1.1/24"), fvSubnetNode)


def _static_infra_chain(aci: Niwaki) -> None:
    """infra (singleton) → leaf profile (name) → leaf selector (mixed kwargs) → node block."""
    infra = aci.infra()
    assert_type(infra, infraInfraNode)
    assert_type(infra.leaf_profile("leaf101"), infraNodePNode)
    assert_type(infra.leaf_profile(), Query[infraNodeP])
    profile = infra.leaf_profile("leaf101")
    # One required naming prop (name) plus one optional (selector_type).
    assert_type(profile.leaf_selector(name="s1"), infraLeafSNode)
    assert_type(profile.leaf_selector(name="s1", selector_type="range"), infraLeafSNode)
    assert_type(profile.leaf_selector(), Query[infraLeafS])
    # The node block has only the universal edges: a leaf.
    selector = profile.leaf_selector(name="s1")
    assert_type(selector.node_block("b1"), NavLeaf[infraNodeBlk])
    assert_type(selector.node_block(), Query[infraNodeBlk])
    # Mixed kwargs on a pool: name required, allocation_mode optional.
    assert_type(infra.vlan_pool(name="vlans"), fvnsVlanInstPNode)
    assert_type(infra.vlan_pool(name="vlans", allocation_mode="static"), fvnsVlanInstPNode)
    assert_type(infra.vlan_pool(), Query[fvnsVlanInstP])


def _static_fabric_chain(aci: Niwaki) -> None:
    """fabric (singleton) → datetime policy (name) → NTP provider (name)."""
    fabric = aci.fabric()
    assert_type(fabric, fabricInstNode)
    policy = fabric.datetime_policy("ntp")
    assert_type(policy, datetimePolNode)
    assert_type(fabric.datetime_policy(), Query[datetimePol])
    assert_type(policy.ntp_provider("10.0.0.1"), datetimeNtpProvNode)
    assert_type(policy.ntp_provider(), Query[datetimeNtpProv])


def _static_all_optional_kwargs(aci: Niwaki) -> None:
    """Every naming prop optional: no argument → Query, any one prop → node."""
    # One optional prop, at the root.
    assert_type(aci.vmm_provider(vendor="VMware"), vmmProvPNode)
    assert_type(aci.vmm_provider(), Query[vmmProvP])
    assert_type(aci.vmm_provider(vendor="VMware").vmm_dom("dc1"), vmmDomPNode)
    # One optional prop, deep in the fabric.
    policy = aci.fabric().datetime_policy("ntp")
    assert_type(policy.ntp_auth_key(auth_key_id=1), NavLeaf[datetimeNtpAuthKey])
    assert_type(policy.ntp_auth_key(), Query[datetimeNtpAuthKey])
    # Four optional props: any single one, or any combination, reaches the node.
    fabric = aci.fabric()
    assert_type(fabric.fabric_port_port(node=101), NavLeaf[fabricPortConfig])
    assert_type(fabric.fabric_port_port(sub_port=0), NavLeaf[fabricPortConfig])
    assert_type(
        fabric.fabric_port_port(node=101, card=1, port=1, sub_port=0), NavLeaf[fabricPortConfig]
    )
    assert_type(fabric.fabric_port_port(), Query[fabricPortConfig])


def _static_singleton_shapes(aci: Niwaki) -> None:
    """Singleton ``()`` and Rs-singleton ``(target_name="", **kwargs)`` → node."""
    bd = aci.tenant("prod").bd("web")
    assert_type(bd.pim(), pimBDPNode)
    assert_type(bd.legacy_mode(), NavLeaf[fvAccP])
    assert_type(bd.vrf_binding(), fvRsCtxNode)
    assert_type(bd.vrf_binding("prod"), fvRsCtxNode)
    assert_type(bd.vrf_binding(target_name="prod"), fvRsCtxNode)
    subnet = bd.subnet(subnet="10.0.0.1/24")
    assert_type(subnet.nd_prefix_policy("ra"), NavLeaf[fvRsNdPfxPol])
    assert_type(subnet.nd_prefix_policy(), NavLeaf[fvRsNdPfxPol])


def _static_universal_edges(aci: Niwaki) -> None:
    """``tag``/``annotation`` on every node family; their targets are ends."""
    bd = aci.tenant("prod").bd("web")
    assert_type(bd.tag(key="owner"), NavEnd[tagTag])
    assert_type(bd.tag(), Query[tagTag])
    assert_type(bd.annotation(key="team"), NavEnd[tagAnnotation])
    # A leaf carries the universal edges too.
    assert_type(bd.legacy_mode().tag(key="owner"), NavEnd[tagTag])
    # So does the root, from the client and from the explicit root node.
    assert_type(aci.tag(key="owner"), NavEnd[tagTag])
    assert_type(aci.root.annotation(key="team"), NavEnd[tagAnnotation])


def _static_root(aci: Niwaki) -> None:
    """``aci.root`` is the typed root node; the client proxies the same edges."""
    assert_type(aci.root, RootNode)
    assert_type(aci.root.tenant("prod").bd("web"), fvBDNode)
    assert_type(aci.root.infra(), infraInfraNode)
    assert_type(aci.root.tenant(), Query[fvTenant])
    # Required kwargs at the root, both props under their Python names.
    assert_type(
        aci.ndo_controller_profile(ndo_ip="10.0.0.9", ndo_name="ndo"), NavLeaf[polNdoProfile]
    )
    assert_type(aci.ndo_controller_profile(), Query[polNdoProfile])


def _static_read_is_concrete(aci: Niwaki) -> None:
    """``read()`` returns the concrete model, inherited from ``NiwakiNode[T]``."""
    assert_type(aci.tenant("prod").read(), fvTenant)
    assert_type(aci.tenant("prod").bd("web").read(), fvBD)
    assert_type(aci.tenant("prod").app("shop").read(), fvAp)
    assert_type(aci.tenant("prod").bd("web").subnet(subnet="10.0.0.1/24").read(), fvSubnet)
    assert_type(aci.tenant("prod").bd("web").legacy_mode().read(), fvAccP)
    assert_type(aci.tenant("prod").bd("web").vrf_binding("prod").read(), fvRsCtx)
    assert_type(aci.tenant("prod").bd("web").tag(key="owner").read(), tagTag)
    assert_type(aci.infra().read(), infraInfra)
    assert_type(aci.fabric().read(), fabricInst)
    assert_type(aci.vmm_provider(vendor="VMware").vmm_dom("dc1").read(), vmmDomP)
    # The node API stays reachable on a typed node.
    assert_type(aci.tenant("prod").bd("web").dn, str)
    assert_type(aci.tenant("prod").bd("web").cls, type[fvBD])
    assert_type(aci.tenant("prod").bd("web").query(fvSubnet), Query[fvSubnet])
    aci.tenant("prod").bd("web").delete()


def _static_dynamic_escape_hatches(aci: Niwaki) -> None:
    """``node()``/``mo()`` stay dynamic: ``NiwakiNode[T]``, any attribute, no error."""
    node = aci.node("uni/tn-prod/BD-web", fvBD)
    assert_type(node, NiwakiNode[fvBD])
    assert_type(node.read(), fvBD)
    node.anything_at_all("x")  # resolved at runtime — never a static error
    child = aci.tenant("prod").mo(fvBD, name="web")
    assert_type(child, NiwakiNode[fvBD])
    child.subnet(subnet="10.0.0.1/24").whatever()


# ── 2. Expected errors, sync ──────────────────────────────────────────────────


def _static_typos(aci: Niwaki) -> None:
    """A name that is not an edge is an error on every typed node family."""
    aci.tenantt("prod")  # type: ignore[attr-defined]  # expect: reportAttributeAccessIssue
    aci.root.tenantt("prod")  # type: ignore[operator]  # expect: reportCallIssue
    aci.tenant("prod").bdd("web")  # type: ignore[operator]  # expect: reportCallIssue
    aci.infra().leaf_profil("leaf101")  # type: ignore[operator]  # expect: reportCallIssue
    aci.fabric().datetime_polcy("ntp")  # type: ignore[operator]  # expect: reportCallIssue
    bd = aci.tenant("prod").bd("web")
    bd.legacy_mode().subnet()  # type: ignore[operator]  # expect: reportCallIssue
    bd.tag(key="owner").tag()  # type: ignore[operator]  # expect: reportCallIssue


def _static_wire_aliases(aci: Niwaki) -> None:
    """Wire aliases are refused statically: the typed surface speaks Python names."""
    bd = aci.tenant("prod").bd("web")
    bd.subnet(ip="10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    epg = aci.tenant("prod").app("shop").epg("web")
    epg.static_path(tDn=_TDN)  # type: ignore[call-overload]  # expect: reportCallIssue
    profile = aci.infra().leaf_profile("leaf101")
    profile.leaf_selector(name="s1", type="range")  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.infra().vlan_pool(name="v", allocMode="static")  # type: ignore[call-overload]  # expect: reportCallIssue
    policy = aci.fabric().datetime_policy("ntp")
    policy.ntp_auth_key(id=1)  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.fabric().fabric_port_port(subPort=0)  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.ndo_controller_profile(ip="10.0.0.9", name="ndo")  # type: ignore[call-overload]  # expect: reportCallIssue


def _static_wrong_arguments(aci: Niwaki) -> None:
    """Shapes are enforced: positional kwargs, missing props, extra or mistyped args."""
    bd = aci.tenant("prod").bd("web")
    bd.subnet("10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.ndo_controller_profile(ndo_ip="10.0.0.9")  # type: ignore[call-overload]  # expect: reportCallIssue
    bd.pim("x")  # type: ignore[call-arg]  # expect: reportCallIssue
    aci.infra("x")  # type: ignore[call-arg]  # expect: reportCallIssue
    aci.tenant(42)  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    bd.vrf_binding(42)  # type: ignore[arg-type]  # expect: reportArgumentType


# ── 1+2. The same contract, async ─────────────────────────────────────────────


async def _static_async_chains(aci: AsyncNiwaki, bd_name: str) -> None:
    """The async facade mirrors every shape with async nodes and ``AsyncQuery``."""
    assert_type(aci.tenant("prod"), AsyncfvTenantNode)
    assert_type(aci.tenant(), AsyncQuery[fvTenant])
    assert_type(aci.root, AsyncRootNode)
    assert_type(aci.root.tenant("prod"), AsyncfvTenantNode)
    bd = aci.tenant("prod").bd(bd_name)
    assert_type(bd, AsyncfvBDNode)
    assert_type(aci.tenant("prod").bd(), AsyncQuery[fvBD])
    assert_type(bd.subnet(subnet="10.0.0.1/24"), AsyncfvSubnetNode)
    assert_type(bd.subnet(), AsyncQuery[fvSubnet])
    app = aci.tenant("prod").app("shop")
    assert_type(app, AsyncfvApNode)
    assert_type(app.epg("web"), AsyncfvAEPgNode)
    assert_type(app.epg("web").static_path(target_dn=_TDN), AsyncfvRsPathAttNode)
    # infra
    infra = aci.infra()
    assert_type(infra, AsyncinfraInfraNode)
    profile = infra.leaf_profile("leaf101")
    assert_type(profile, AsyncinfraNodePNode)
    assert_type(profile.leaf_selector(name="s1"), AsyncinfraLeafSNode)
    assert_type(profile.leaf_selector(name="s1").node_block("b1"), AsyncNavLeaf[infraNodeBlk])
    assert_type(infra.vlan_pool(name="vlans"), AsyncfvnsVlanInstPNode)
    # fabric
    fabric = aci.fabric()
    assert_type(fabric, AsyncfabricInstNode)
    assert_type(fabric.datetime_policy("ntp"), AsyncdatetimePolNode)
    assert_type(fabric.datetime_policy("ntp").ntp_provider("10.0.0.1"), AsyncdatetimeNtpProvNode)
    # all-optional kwargs
    assert_type(aci.vmm_provider(vendor="VMware"), AsyncvmmProvPNode)
    assert_type(aci.vmm_provider(), AsyncQuery[vmmProvP])
    assert_type(aci.vmm_provider(vendor="VMware").vmm_dom("dc1"), AsyncvmmDomPNode)
    assert_type(fabric.fabric_port_port(port=1), AsyncNavLeaf[fabricPortConfig])
    assert_type(fabric.fabric_port_port(), AsyncQuery[fabricPortConfig])
    # singletons, leaves, ends
    assert_type(bd.pim(), AsyncpimBDPNode)
    assert_type(bd.legacy_mode(), AsyncNavLeaf[fvAccP])
    assert_type(bd.vrf_binding("prod"), AsyncfvRsCtxNode)
    assert_type(bd.tag(key="owner"), AsyncNavEnd[tagTag])
    assert_type(aci.tag(key="owner"), AsyncNavEnd[tagTag])
    assert_type(
        aci.ndo_controller_profile(ndo_ip="10.0.0.9", ndo_name="ndo"), AsyncNavLeaf[polNdoProfile]
    )
    # read() is a coroutine of the concrete model.
    assert_type(await bd.read(), fvBD)
    assert_type(await bd.subnet(subnet="10.0.0.1/24").read(), fvSubnet)
    assert_type(await infra.read(), infraInfra)
    assert_type(await bd.legacy_mode().read(), fvAccP)
    # Dynamic escape hatches stay dynamic.
    node = aci.node("uni/tn-prod/BD-web", fvBD)
    assert_type(node, AsyncNiwakiNode[fvBD])
    node.anything_at_all("x")


async def _static_async_errors(aci: AsyncNiwaki) -> None:
    """Every sync error family holds on the async facade."""
    aci.tenantt("prod")  # type: ignore[attr-defined]  # expect: reportAttributeAccessIssue
    aci.root.tenantt("prod")  # type: ignore[operator]  # expect: reportCallIssue
    bd = aci.tenant("prod").bd("web")
    bd.subnett(subnet="x")  # type: ignore[operator]  # expect: reportCallIssue
    aci.infra().leaf_profil("leaf101")  # type: ignore[operator]  # expect: reportCallIssue
    bd.legacy_mode().subnet()  # type: ignore[operator]  # expect: reportCallIssue
    bd.subnet(ip="10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.fabric().fabric_port_port(subPort=0)  # type: ignore[call-overload]  # expect: reportCallIssue
    bd.subnet("10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    bd.pim("x")  # type: ignore[call-arg]  # expect: reportCallIssue


# ── Checker drivers ───────────────────────────────────────────────────────────


def test_mypy_accepts_static_contract() -> None:
    """mypy (strict, the repo config) accepts this file: shapes hold, ignores are used."""
    api = pytest.importorskip("mypy.api")
    stdout, stderr, status = api.run(
        ["--config-file", str(_REPO / "pyproject.toml"), str(Path(__file__))]
    )
    assert status == 0, stdout + stderr


_EXPECT = re.compile(r"# expect: ([\w, ]+)$")


def _expected_pyright_errors() -> set[tuple[int, str]]:
    source = Path(__file__)
    return {
        (number, rule.strip())
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1)
        if (match := _EXPECT.search(line))
        for rule in match.group(1).split(",")
    }


def test_every_type_ignore_carries_a_pyright_expectation() -> None:
    """The two pins stay paired: no mypy-only ignore, no pyright-only marker."""
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    ignores = {n for n, line in enumerate(lines, 1) if re.search(r"\)  # type: ignore\[", line)}
    markers = {n for n, _rule in _expected_pyright_errors()}
    assert ignores == markers
    assert len(ignores) >= 30, "the error probes were thinned out"


def test_pyright_pins_expected_errors(tmp_path: Path) -> None:
    """With type-ignore comments off, pyright reports exactly the ``# expect:`` rules."""
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright is not installed")
    source = Path(__file__)
    expected = _expected_pyright_errors()
    # The repo config (JSONC: full-line // comments only), minus the parts
    # that are relative to the repo root, plus type-ignore comments disabled.
    raw = (_REPO / "pyrightconfig.json").read_text(encoding="utf-8")
    config = json.loads(
        "\n".join(row for row in raw.splitlines() if not row.strip().startswith("//"))
    )
    for key in ("include", "exclude", "executionEnvironments"):
        config.pop(key, None)
    config |= {"venvPath": str(_REPO), "enableTypeIgnoreComments": False}
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
    run = subprocess.run(
        [pyright, "--outputjson", "-p", str(tmp_path / "pyrightconfig.json"), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(run.stdout)
    actual = {
        (diag["range"]["start"]["line"] + 1, diag.get("rule", "?"))
        for diag in report["generalDiagnostics"]
        if diag["severity"] == "error"
    }
    assert actual == expected


# ── 3. Runtime side ───────────────────────────────────────────────────────────


def test_navigation_still_returns_plain_nodes() -> None:
    """The typed classes are a checker fiction: runtime nodes are ``NiwakiNode``."""
    aci = Niwaki("https://apic.invalid", "admin", "secret")
    tenant = aci.tenant("x")
    assert type(tenant) is NiwakiNode
    bd = tenant.bd("web")
    assert type(bd) is NiwakiNode
    assert type(bd.subnet(subnet="10.0.0.1/24")) is NiwakiNode
    assert type(bd.vrf_binding("prod")) is NiwakiNode
    assert type(bd.legacy_mode()) is NiwakiNode
    assert type(bd.tag(key="owner")) is NiwakiNode
    assert type(aci.infra().leaf_profile("leaf101").leaf_selector(name="s1")) is NiwakiNode
    assert type(aci.fabric().fabric_port_port(node=101)) is NiwakiNode
    assert type(aci.root) is NiwakiNode


def test_async_navigation_still_returns_plain_nodes() -> None:
    """Same invariant on the async facade."""
    aci = AsyncNiwaki("https://apic.invalid", "admin", "secret")
    assert type(aci.tenant("x")) is AsyncNiwakiNode
    assert type(aci.tenant("x").bd("web")) is AsyncNiwakiNode
    assert type(aci.infra().leaf_profile("leaf101")) is AsyncNiwakiNode
    assert type(aci.root) is AsyncNiwakiNode


def test_wire_aliases_still_resolve_at_runtime() -> None:
    """Refused statically, still accepted at runtime: the stubs change no behaviour."""
    aci = Niwaki("https://apic.invalid", "admin", "secret")
    bd = aci.tenant("prod").bd("web")
    by_alias = bd.subnet(ip="10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    assert (
        by_alias.dn
        == bd.subnet(subnet="10.0.0.1/24").dn
        == "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]"
    )


def test_facade_bases_are_unchanged() -> None:
    """The ``TYPE_CHECKING`` base hook resolves to :class:`object` at runtime."""
    assert Niwaki.__bases__ == (object,)
    assert AsyncNiwaki.__bases__ == (object,)
    assert Niwaki.__mro__ == (Niwaki, object)
    assert AsyncNiwaki.__mro__ == (AsyncNiwaki, object)
    # The client's navigation proxy still exists at runtime (hidden from checkers only).
    assert "__getattr__" in vars(Niwaki)
    assert "__getattr__" in vars(AsyncNiwaki)


def test_typed_navigation_package_is_not_importable() -> None:
    """``niwaki._generated_nav`` holds ``.pyi`` stubs only — nothing to import.

    A directory without ``__init__.py`` is still importable as an (empty)
    *namespace* package, so the directory itself imports; what must never
    resolve is any of its names or submodules.  Nothing in niwaki imports it.
    """
    import niwaki

    assert "niwaki._generated_nav" not in sys.modules
    try:
        with pytest.raises(ImportError):
            exec("from niwaki._generated_nav import RootNav", {})
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("niwaki._generated_nav.fv")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("niwaki._generated_nav._root")
    finally:
        sys.modules.pop("niwaki._generated_nav", None)
        if hasattr(niwaki, "_generated_nav"):
            delattr(niwaki, "_generated_nav")


def test_typed_navigation_package_ships_stubs_only() -> None:
    """The directory exists, holds ``.pyi`` files, and not a single ``.py``."""
    import niwaki

    nav_dir = Path(niwaki.__file__).parent / "_generated_nav"
    assert nav_dir.is_dir(), f"{nav_dir} is missing — run the navigation stub generator"
    assert not list(nav_dir.rglob("*.py")), "a .py in _generated_nav would make it importable"
    stubs = {path.name for path in nav_dir.glob("*.pyi")}
    assert {"__init__.pyi", "_base.pyi", "_root.pyi", "fv.pyi"} <= stubs
