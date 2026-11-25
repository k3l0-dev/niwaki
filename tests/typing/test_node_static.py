# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Typed ``node(dn, cls)`` / ``mo(cls)``: the static contract (lot N2).

Since 2.1 vocabulary navigation is typed (``aci.tenant("x").bd("web")`` is a
``fvBDNode``), but the two explicit-class entry points were not:
``aci.node(dn, fvBD)`` and ``node.mo(fvBD, name="web")`` returned a plain
``NiwakiNode[fvBD]`` whose ``__getattr__`` is ``Any`` — no completion, no
typo detection — and ``aci.node("uni")`` even read as ``NiwakiNode[Never]``
under mypy (the ``cls: type[T] = ManagedObject`` default).

The contract pinned here, sync and async:

* ``aci.node(dn, C)`` and ``<any node>.mo(C, ...)`` return **the same typed
  navigation class vocabulary navigation returns for C**: ``<C>Node`` for a
  class with specific edges, ``NavLeaf[C]`` / ``NavEnd[C]`` otherwise, and
  ``RootNode`` for ``polUni`` (whose vocabulary is the root row).  So
  ``aci.node("uni/tn-x", fvTenant).bd("web")`` is a ``fvBDNode`` and
  ``.tenant("y")`` on it is a static error.
* ``aci.root`` is the ``polUni`` node: ``read()`` is a ``polUni``.
* ``aci.node(dn)`` without a class is **honestly dynamic**:
  ``NiwakiNode[ManagedObject]`` (never ``Never``), any attribute allowed —
  the class is only known at runtime (resolved from the DN).
* :func:`test_every_generated_class_maps_to_its_navigation_class` checks
  the mapping for **every** generated class (both flavours) under mypy and
  pyright, against ``CHILD_MAP`` — the single authority — so no class can be
  mapped to a sealed navigation class that misses one of its edges.

Expected errors use the ``# type: ignore[code]  # expect: <pyright rules>``
convention of ``test_nav_static.py`` (see that module for the rationale).

Typing mechanism (spiked 2026-10-27, wheel installed in a clean venv,
mypy 2.4.0 strict, pyright 1.1.4xx; user project calling ``node()``/``mo()``)
--------------------------------------------------------------------------------

**Chosen: a per-model, checker-only class attribute + a protocol.**  Each
generated model declares, under ``if TYPE_CHECKING:``, a ``ClassVar`` naming
its sync and async navigation classes (``ClassVar[fvBDNode]`` /
``ClassVar[AsyncfvBDNode]``); ``node()``/``mo()`` carry three overloads —
no class → ``NiwakiNode[ManagedObject]``; ``cls: _Navigable[N]`` (a
``Protocol`` with that attribute) → ``N``; ``cls: type[T]`` →
``NiwakiNode[T]`` (``ManagedObject`` itself) — sync and async.  Zero runtime
change (the attribute is never assigned; nodes keep their class and MRO).
The overloads live on the real ``facade.py`` methods: a checker-only *base*
class for ``NiwakiNode`` is not possible without changing its runtime MRO.

==============================  ===============  ===========================
                                mypy cold        pyright (files bound)
==============================  ===============  ===========================
2.1 (base)                      4.2-4.6 s        0.9-1.5 s (520)
**protocol (chosen)**           4.4-4.6 s        0.9-1.8 s (518)
2 221 overloads per method      5.0-5.5 s        3.5-6.8 s (3 174)
==============================  ===============  ===========================

The overload alternative (one ``@overload`` per generated class on each of
``node``/``mo``, sync and async: 8 888 overloads) costs mypy ≈ +1 s cold, but
pyright resolves every overload parameter's class on each call — the
2 221 model modules are parsed on the first ``node()``/``mo()`` call, which
Pylance users would feel in the editor; the repo-side mypy run would also pay
mypy's quadratic overload-overlap check (skipped only for installed
packages).  The protocol costs nothing measurable on either checker.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

import pytest

from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG

if TYPE_CHECKING:
    from niwaki._generated_nav import (
        AsyncNavEnd,
        AsyncNavLeaf,
        AsyncRootNode,
        NavEnd,
        NavLeaf,
        RootNode,
    )
    from niwaki._generated_nav.fv import (
        AsyncfvBDNode,
        AsyncfvRsCtxNode,
        AsyncfvSubnetNode,
        AsyncfvTenantNode,
        fvBDNode,
        fvRsCtxNode,
        fvSubnetNode,
        fvTenantNode,
    )
    from niwaki._generated_nav.infra import (
        AsyncinfraInfraNode,
        AsyncinfraNodePNode,
        infraInfraNode,
        infraNodePNode,
    )
    from niwaki.models.base import ManagedObject
    from niwaki.models.fv.fvAccP import fvAccP
    from niwaki.models.fv.fvBD import fvBD
    from niwaki.models.fv.fvRsCtx import fvRsCtx
    from niwaki.models.fv.fvSubnet import fvSubnet
    from niwaki.models.fv.fvTenant import fvTenant
    from niwaki.models.infra.infraInfra import infraInfra
    from niwaki.models.infra.infraNodeBlk import infraNodeBlk
    from niwaki.models.pol.polUni import polUni
    from niwaki.models.tag.tagTag import tagTag
    from niwaki.query import AsyncQuery, Query

_REPO = Path(__file__).resolve().parents[2]

_BLK = "uni/infra/nprof-leaf101/leaves-s1-typ-range/nodeblk-b1"


# ── 1. Static side, sync ──────────────────────────────────────────────────────


def _static_node_with_class(aci: Niwaki) -> None:
    """``node(dn, C)`` is the typed navigation class of C — every family."""
    tenant = aci.node("uni/tn-prod", fvTenant)
    assert_type(tenant, fvTenantNode)
    assert_type(tenant.bd("web"), fvBDNode)
    assert_type(tenant.bd(), Query[fvBD])
    assert_type(tenant.read(), fvTenant)
    assert_type(tenant.cls, type[fvTenant])
    bd = aci.node("uni/tn-prod/BD-web", fvBD)
    assert_type(bd, fvBDNode)
    assert_type(bd.subnet(subnet="10.0.0.1/24"), fvSubnetNode)
    assert_type(bd.vrf_binding(), fvRsCtxNode)
    assert_type(bd.query(fvSubnet), Query[fvSubnet])
    assert_type(aci.node("uni/infra", infraInfra), infraInfraNode)
    assert_type(aci.node("uni/infra", infraInfra).leaf_profile("leaf101"), infraNodePNode)
    # The two generic families: universal edges only, no edge at all.
    assert_type(aci.node(_BLK, infraNodeBlk), NavLeaf[infraNodeBlk])
    assert_type(aci.node("uni/tn-prod/tagKey-owner", tagTag), NavEnd[tagTag])
    assert_type(aci.node(_BLK, infraNodeBlk).read(), infraNodeBlk)


def _static_policy_universe_is_the_root(aci: Niwaki) -> None:
    """``polUni``'s vocabulary is the root row: its node is ``RootNode``."""
    root = aci.node("uni", polUni)
    assert_type(root, RootNode)
    assert_type(root.tenant("prod"), fvTenantNode)
    assert_type(root.tenant(), Query[fvTenant])
    assert_type(root.read(), polUni)
    assert_type(aci.root, RootNode)
    assert_type(aci.root.read(), polUni)
    assert_type(aci.root.cls, type[polUni])


def _static_mo_with_class(aci: Niwaki) -> None:
    """``mo(C, ...)`` is typed from every node: typed, root, untyped."""
    assert_type(aci.tenant("prod").mo(fvBD, name="web"), fvBDNode)
    assert_type(aci.tenant("prod").mo(fvBD, name="web").subnet(subnet="x"), fvSubnetNode)
    assert_type(aci.root.mo(fvTenant, name="prod"), fvTenantNode)
    assert_type(aci.root.mo(fvTenant, name="prod").bd("web"), fvBDNode)
    assert_type(aci.tenant("prod").bd("web").mo(fvAccP), NavLeaf[fvAccP])
    assert_type(aci.tenant("prod").bd("web").mo(fvRsCtx).read(), fvRsCtx)
    # An untyped parent still yields a typed child: the class is stated.
    assert_type(aci.node("uni/tn-prod").mo(fvBD, name="web"), fvBDNode)
    assert_type(aci.node("uni/tn-prod").mo(fvBD, name="web").read(), fvBD)
    # Chains through node(dn, C) and mo(C) compose.
    chained = aci.node("uni/tn-prod", fvTenant).mo(fvBD, name="web").mo(fvSubnet, ip="x")
    assert_type(chained, fvSubnetNode)


def _static_node_without_class_is_dynamic(aci: Niwaki) -> None:
    """No class: ``NiwakiNode[ManagedObject]`` — never ``Never`` — and any attribute."""
    assert_type(aci.node("uni"), NiwakiNode[ManagedObject])
    node = aci.node("uni/tn-prod")
    assert_type(node, NiwakiNode[ManagedObject])
    assert_type(node.read(), ManagedObject)
    assert_type(node.query(fvBD), Query[fvBD])
    assert_type(node.dn, str)
    node.bd("web").subnet(subnet="10.0.0.1/24")  # resolved at runtime — no static error
    node.anything_at_all("x")


# ── 2. Expected errors, sync ──────────────────────────────────────────────────


def _static_typed_node_errors(aci: Niwaki) -> None:
    """A typed node rejects names its class does not have — the root's included."""
    tenant = aci.node("uni/tn-prod", fvTenant)
    tenant.tenant("other")  # type: ignore[operator]  # expect: reportCallIssue
    tenant.bdd("web")  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni", polUni).tenantt("prod")  # type: ignore[operator]  # expect: reportCallIssue
    aci.root.bd("web")  # type: ignore[operator]  # expect: reportCallIssue
    bd = aci.node("uni/tn-prod/BD-web", fvBD)
    bd.subnet(ip="10.0.0.1/24")  # type: ignore[call-overload]  # expect: reportCallIssue
    bd.pim("x")  # type: ignore[call-arg]  # expect: reportCallIssue
    aci.node(_BLK, infraNodeBlk).subnet()  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni/tn-prod/tagKey-owner", tagTag).tag()  # type: ignore[operator]  # expect: reportCallIssue


def _static_typed_mo_errors(aci: Niwaki) -> None:
    """The child of ``mo(C)`` is sealed like any typed node."""
    aci.tenant("prod").mo(fvBD, name="web").tenant("x")  # type: ignore[operator]  # expect: reportCallIssue
    aci.root.mo(fvTenant, name="prod").vrff("x")  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni/tn-prod").mo(fvBD, name="web").subnett()  # type: ignore[operator]  # expect: reportCallIssue
    aci.root.mo(polUni).bd("web")  # type: ignore[operator]  # expect: reportCallIssue


# ── 1+2. The same contract, async ─────────────────────────────────────────────


async def _static_async(aci: AsyncNiwaki) -> None:
    tenant = aci.node("uni/tn-prod", fvTenant)
    assert_type(tenant, AsyncfvTenantNode)
    assert_type(tenant.bd("web"), AsyncfvBDNode)
    assert_type(tenant.bd(), AsyncQuery[fvBD])
    assert_type(await tenant.read(), fvTenant)
    bd = aci.node("uni/tn-prod/BD-web", fvBD)
    assert_type(bd, AsyncfvBDNode)
    assert_type(bd.subnet(subnet="10.0.0.1/24"), AsyncfvSubnetNode)
    assert_type(bd.vrf_binding(), AsyncfvRsCtxNode)
    assert_type(aci.node("uni/infra", infraInfra), AsyncinfraInfraNode)
    assert_type(aci.node("uni/infra", infraInfra).leaf_profile("l"), AsyncinfraNodePNode)
    assert_type(aci.node(_BLK, infraNodeBlk), AsyncNavLeaf[infraNodeBlk])
    assert_type(aci.node("uni/tn-prod/tagKey-owner", tagTag), AsyncNavEnd[tagTag])
    # polUni is the root.
    assert_type(aci.node("uni", polUni), AsyncRootNode)
    assert_type(aci.node("uni", polUni).tenant("prod"), AsyncfvTenantNode)
    assert_type(aci.root, AsyncRootNode)
    assert_type(await aci.root.read(), polUni)
    # mo() from every kind of node.
    assert_type(aci.tenant("prod").mo(fvBD, name="web"), AsyncfvBDNode)
    assert_type(aci.root.mo(fvTenant, name="prod").bd("web"), AsyncfvBDNode)
    assert_type(aci.node("uni/tn-prod").mo(fvBD, name="web"), AsyncfvBDNode)
    assert_type(aci.tenant("prod").bd("web").mo(fvAccP), AsyncNavLeaf[fvAccP])
    assert_type(await aci.tenant("prod").mo(fvBD, name="web").read(), fvBD)
    # No class: honestly dynamic.
    assert_type(aci.node("uni"), AsyncNiwakiNode[ManagedObject])
    untyped = aci.node("uni/tn-prod")
    assert_type(untyped, AsyncNiwakiNode[ManagedObject])
    assert_type(await untyped.read(), ManagedObject)
    untyped.bd("web").anything_at_all()


async def _static_async_errors(aci: AsyncNiwaki) -> None:
    tenant = aci.node("uni/tn-prod", fvTenant)
    tenant.tenant("other")  # type: ignore[operator]  # expect: reportCallIssue
    tenant.bdd("web")  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni", polUni).tenantt("prod")  # type: ignore[operator]  # expect: reportCallIssue
    aci.root.bd("web")  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni/tn-prod/BD-web", fvBD).subnet(ip="x")  # type: ignore[call-overload]  # expect: reportCallIssue
    aci.node("uni/tn-prod/tagKey-owner", tagTag).tag()  # type: ignore[operator]  # expect: reportCallIssue
    aci.tenant("prod").mo(fvBD, name="web").tenant("x")  # type: ignore[operator]  # expect: reportCallIssue
    aci.node("uni/tn-prod").mo(fvBD, name="web").subnett()  # type: ignore[operator]  # expect: reportCallIssue


def _static_instance_is_not_a_class(aci: Niwaki) -> None:
    """A model *instance* where the class is expected is refused by both checkers.

    The runtime raises ``TypeError`` for it; the class-object protocol must not
    let an instance through (a ``ClassVar`` is readable on an instance too).
    """
    aci.node("uni/tn-x/BD-b", fvBD(name="b"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.tenant("x").mo(fvTenant(name="y"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.root.mo(fvTenant(name="y"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.node("uni/tn-x").mo(fvBD(name="b"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType


async def _static_async_instance_is_not_a_class(aci: AsyncNiwaki) -> None:
    aci.node("uni/tn-x/BD-b", fvBD(name="b"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.tenant("x").mo(fvTenant(name="y"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.root.mo(fvTenant(name="y"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType
    aci.node("uni/tn-x").mo(fvBD(name="b"))  # type: ignore[call-overload]  # expect: reportCallIssue, reportArgumentType


def _static_positive_probes_survive_the_instance_fix(aci: Niwaki) -> None:
    """The class keeps mapping to its node — and ``ManagedObject`` to the plain node."""
    assert_type(aci.node("uni/tn-x/BD-b", fvBD), fvBDNode)
    assert_type(aci.tenant("x").mo(fvBD, name="b"), fvBDNode)
    assert_type(aci.node("uni/tn-x", ManagedObject), NiwakiNode[ManagedObject])
    assert_type(aci.tenant("x").mo(ManagedObject), NiwakiNode[ManagedObject])


# ── Checker drivers ───────────────────────────────────────────────────────────


def _run_mypy(path: Path) -> None:
    api = pytest.importorskip("mypy.api")
    stdout, stderr, status = api.run(["--config-file", str(_REPO / "pyproject.toml"), str(path)])
    assert status == 0, stdout + stderr


def _pyright_errors(path: Path, tmp_path: Path) -> set[tuple[int, str]]:
    """(line, rule) of every pyright error, type-ignore comments disabled."""
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright is not installed")
    raw = (_REPO / "pyrightconfig.json").read_text(encoding="utf-8")
    config = json.loads(
        "\n".join(row for row in raw.splitlines() if not row.strip().startswith("//"))
    )
    for key in ("include", "exclude", "executionEnvironments"):
        config.pop(key, None)
    config |= {"venvPath": str(_REPO), "enableTypeIgnoreComments": False}
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
    run = subprocess.run(
        [pyright, "--outputjson", "-p", str(tmp_path / "pyrightconfig.json"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(run.stdout)
    return {
        (diag["range"]["start"]["line"] + 1, diag.get("rule", "?"))
        for diag in report["generalDiagnostics"]
        if diag["severity"] == "error"
    }


_EXPECT = re.compile(r"# expect: ([\w, ]+)$")


def _expected_pyright_errors() -> set[tuple[int, str]]:
    return {
        (number, rule.strip())
        for number, line in enumerate(
            Path(__file__).read_text(encoding="utf-8").splitlines(), start=1
        )
        if (match := _EXPECT.search(line))
        for rule in match.group(1).split(",")
    }


def test_mypy_accepts_static_contract() -> None:
    """mypy (strict, the repo config) accepts this file: shapes hold, ignores are used."""
    _run_mypy(Path(__file__))


def test_every_type_ignore_carries_a_pyright_expectation() -> None:
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    ignores = {n for n, line in enumerate(lines, 1) if re.search(r"\)  # type: ignore\[", line)}
    markers = {n for n, _rule in _expected_pyright_errors()}
    assert ignores == markers
    assert len(ignores) >= 20, "the error probes were thinned out"


def test_pyright_pins_expected_errors(tmp_path: Path) -> None:
    """With type-ignore comments off, pyright reports exactly the ``# expect:`` rules."""
    assert _pyright_errors(Path(__file__), tmp_path) == _expected_pyright_errors()


# ── Exhaustive mapping: every generated class, both flavours ──────────────────

_UNIVERSAL = frozenset({"annotation", "tag", "annotation_to_capture_rbac_info"})


def _expected_node_type(cls: str, prefix: str) -> str:
    """The navigation class ``CHILD_MAP`` implies for *cls* (``prefix``: ``""``/``"Async"``)."""
    if cls == "polUni":
        return f"{prefix}RootNode"
    row = set(CHILD_MAP.get(cls, {}))
    if row - _UNIVERSAL:
        return f"{prefix}{cls}Node"
    if row:
        return f"{prefix}NavLeaf[{cls}]"
    return f"{prefix}NavEnd[{cls}]"


def _exhaustive_probe() -> str:
    classes = sorted(CLASS_PKG)
    typed = sorted(
        cls for cls in classes if cls != "polUni" and set(CHILD_MAP.get(cls, {})) - _UNIVERSAL
    )
    lines = [
        "# Generated by tests/typing/test_node_static.py — a static probe, never run.",
        "from __future__ import annotations",
        "",
        "from typing import assert_type",
        "",
        "from niwaki import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode",
        "from niwaki._generated_nav import (",
        "    AsyncNavEnd, AsyncNavLeaf, AsyncRootNode, NavEnd, NavLeaf, RootNode,",
        ")",
        "from niwaki.models.base import ManagedObject",
    ]
    lines += [f"from niwaki.models._generated.{CLASS_PKG[c]}.{c} import {c}" for c in classes]
    lines += [
        f"from niwaki._generated_nav.{CLASS_PKG[c]} import {c}Node, Async{c}Node" for c in typed
    ]
    lines += [
        "",
        "",
        "def _probe(aci: Niwaki, aaci: AsyncNiwaki) -> None:",
        "    assert_type(aci.node('uni/x'), NiwakiNode[ManagedObject])",
        "    assert_type(aaci.node('uni/x'), AsyncNiwakiNode[ManagedObject])",
        "    assert_type(aci.node('uni/x', ManagedObject), NiwakiNode[ManagedObject])",
    ]
    for cls in classes:
        lines.append(f"    assert_type(aci.node('uni/x', {cls}), {_expected_node_type(cls, '')})")
        lines.append(
            f"    assert_type(aaci.node('uni/x', {cls}), {_expected_node_type(cls, 'Async')})"
        )
    return "\n".join(lines) + "\n"


def test_exhaustive_probe_covers_every_generated_class() -> None:
    """Sanity of the probe itself: every class, both flavours, all four families."""
    probe = _exhaustive_probe()
    assert probe.count("assert_type(aci.node('uni/x', ") == len(CLASS_PKG) + 1
    assert probe.count("assert_type(aaci.node('uni/x', ") == len(CLASS_PKG)
    for family in ("RootNode)", "fvBDNode)", "NavLeaf[infraNodeBlk])", "NavEnd[tagTag])"):
        assert family in probe


def test_every_generated_class_maps_to_its_navigation_class_mypy(tmp_path: Path) -> None:
    """mypy: ``node(dn, C)`` is the navigation class ``CHILD_MAP`` implies, for all C."""
    probe = tmp_path / "node_probe.py"
    probe.write_text(_exhaustive_probe(), encoding="utf-8")
    _run_mypy(probe)


def test_every_generated_class_maps_to_its_navigation_class_pyright(tmp_path: Path) -> None:
    """pyright: same exhaustive probe, zero error."""
    probe = tmp_path / "node_probe.py"
    probe.write_text(_exhaustive_probe(), encoding="utf-8")
    assert _pyright_errors(probe, tmp_path) == set()


# ── Runtime side ──────────────────────────────────────────────────────────────


def test_typed_entry_points_return_plain_runtime_nodes() -> None:
    """The typed classes are a checker fiction: ``node``/``mo`` still build plain nodes."""
    from niwaki.models._generated.fv.fvBD import fvBD as FvBD
    from niwaki.models._generated.fv.fvTenant import fvTenant as FvTenant

    for client, node_type in (
        (Niwaki("https://apic.invalid", "a", "b"), NiwakiNode),
        (AsyncNiwaki("https://apic.invalid", "a", "b"), AsyncNiwakiNode),
    ):
        tenant = client.node("uni/tn-prod", FvTenant)
        built = [
            type(tenant),
            type(tenant.mo(FvBD, name="web")),
            type(client.root.mo(FvTenant, name="prod")),
        ]
        assert all(kind is node_type for kind in built), built
