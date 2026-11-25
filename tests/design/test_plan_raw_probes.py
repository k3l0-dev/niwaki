"""Plan mode keeps raw nodes off the per-object probe path.

A *raw* design node (``Cursor.raw()`` on a class without a generated model,
``to_design(..., on_unknown="raw")`` in bulk) is served by the catalogue: it
is a bare :class:`~niwaki.models.base.ManagedObject` whose relative name comes
from the declaration, not from a model.  Plan mode diffs the desired tree
against one class-scoped read per declared domain, and pairs the two trees
child by child.  When a raw node fails to pair with its re-read counterpart it
is booked as a create and re-verified by a bare self-GET — one request **per
raw object**, in sequence.  The verdicts stay right (the probe finds the
object and diffs it), but the request count grows linearly with the number of
raw nodes: a brownfield import of a few hundred raw objects costs a few hundred
round trips where typed nodes cost one.

What this file pins:

- **Request count** — the number of GETs a plan issues does not depend on how
  many raw nodes the design declares, and equals what typed nodes cost (one
  class-scoped read for the one declared domain).  A present object is never
  probed individually; an absent one costs exactly its own probe (the existing
  "one GET per absent-subtree root" model), whatever the number of raw
  neighbours.
- **Verdicts** — creates / updates / unchanged are exactly what they were
  before the count was bounded: each raw node is paired with *its own* stored
  object (values are distinct per node, so a mispairing shows), drift is
  attributed to the right DN under its wire name, foreign objects are ignored,
  a typed drift next to raw nodes is still reported.
- **The brownfield path** — ``snapshot.take`` → ``to_design(on_unknown="raw")``
  → ``plan`` is a no-op fabric-wide with a bounded request count.

Everything runs against :class:`niwaki.testing.FakeApic` and its request log,
through both the sync and the async client.  The raw classes are
``fabricPodDhcpServer`` (numeric naming prop) and ``fabricAssociatedSetupP``
(bracketed, slash-bearing RN) — catalogue classes with no generated model —
under the typed first-time-setup pod ``uni/controller/setuppol/setupp-1``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
import pytest

from niwaki import snapshot
from niwaki.design import Cursor, PlanResult, controller, tenant, to_design
from niwaki.design._node import RawDesignNode
from niwaki.testing import FakeApic

#: Raw nodes in the small design / the large design.  Far enough apart that a
#: per-object cost cannot hide in a constant.
SMALL = 5
LARGE = 60

POD_DN = "uni/controller/setuppol/setupp-1"
FIRST_NODE_ID = 101


# ── Design builders ───────────────────────────────────────────────────────────


def _server_dn(index: int) -> str:
    return f"{POD_DN}/poddhcpserver-{FIRST_NODE_ID + index}"


def _pool(index: int) -> str:
    return f"10.{index}.0.0/16"


def _pool_dn(index: int) -> str:
    return f"{POD_DN}/associatedsetupp-[{_pool(index)}]"


def _design(
    servers: int,
    pools: int = 0,
    *,
    server_descr: Mapping[int, str] | None = None,
    skip_servers: frozenset[int] = frozenset(),
    pod_description: str = "",
) -> Cursor:
    """A first-time-setup pod carrying raw DHCP-server and secondary-subnet nodes.

    Every raw node carries a description unique to it (``server-3``,
    ``pool-3``) so that two nodes of one class are never interchangeable: a
    plan that paired them wrongly would report drift.

    Args:
        servers: Number of ``fabricPodDhcpServer`` raw nodes declared.
        pools: Number of ``fabricAssociatedSetupP`` raw nodes declared.
        server_descr: Per-server-index description overriding the default.
        skip_servers: Server indexes left out of the declaration.
        pod_description: Description of the typed pod, when non-empty.
    """
    cfg = controller()
    pod = cfg.fabric_firsttime_setup_policy().pod_setup(1, description=pod_description or None)
    overrides = server_descr or {}
    for index in range(servers):
        if index in skip_servers:
            continue
        pod.raw(
            "fabricPodDhcpServer",
            nodeId=FIRST_NODE_ID + index,
            descr=overrides.get(index, f"server-{index}"),
        )
    for index in range(pools):
        pod.raw("fabricAssociatedSetupP", pool=_pool(index), descr=f"pool-{index}")
    return cfg


def _typed_design(bridge_domains: int) -> Cursor:
    """A tenant of *bridge_domains* typed bridge domains — the cost baseline."""
    cfg = tenant("bench")
    for index in range(bridge_domains):
        cfg.bd(f"bd{index}", description=f"bd-{index}")
    return cfg


def _raw_count(cfg: Cursor) -> int:
    """How many catalogue-served raw nodes the design really declares."""
    return sum(isinstance(node, RawDesignNode) for node in cfg.design_node.root().iter_subtree())


def _pod_dns(servers: int, pools: int = 0) -> set[str]:
    """Every DN of :func:`_design` — domain root, pod chain, raw nodes."""
    return (
        {"uni/controller", "uni/controller/setuppol", POD_DN}
        | {_server_dn(i) for i in range(servers)}
        | {_pool_dn(i) for i in range(pools)}
    )


# ── Observation seam (sync / async) ───────────────────────────────────────────


@dataclass(frozen=True)
class Observed:
    """One plan run: its verdict and the GETs it issued (login excluded)."""

    result: PlanResult
    gets: list[httpx.Request]

    @property
    def scoped_reads(self) -> list[httpx.Request]:
        """The class-scoped subtree reads — the one-per-domain path."""
        return [r for r in self.gets if "query-target" in r.url.params]

    @property
    def probed_dns(self) -> list[str]:
        """DNs hit by a bare self-GET — the per-object path."""
        return [
            r.url.path.removeprefix("/api/mo/").removesuffix(".json")
            for r in self.gets
            if "query-target" not in r.url.params
        ]


Planner = Callable[[FakeApic, Cursor], Awaitable[Observed]]


def _gets_since(fake: FakeApic, since: int) -> list[httpx.Request]:
    return [r for r in fake.requests[since:] if r.method == "GET"]


async def _plan_sync(fake: FakeApic, cfg: Cursor) -> Observed:
    since = len(fake.requests)
    with fake.client() as aci:
        result = cfg.push(aci, mode="plan")
    return Observed(result, _gets_since(fake, since))


async def _plan_async(fake: FakeApic, cfg: Cursor) -> Observed:
    since = len(fake.requests)
    async with fake.async_client() as aci:
        result = await cfg.push(aci, mode="plan")
    return Observed(result, _gets_since(fake, since))


@pytest.fixture(params=["sync", "async"])
def plan(request: pytest.FixtureRequest) -> Planner:
    """The plan seam under test, once through each client flavour."""
    return {"sync": _plan_sync, "async": _plan_async}[str(request.param)]


async def _planned(plan: Planner, desired: Cursor, stored: Cursor | None = None) -> Observed:
    """Plan *desired* against a fresh fake holding *stored* (default: *desired*)."""
    fake = FakeApic()
    fake.seed((stored or desired).to_payload())
    return await plan(fake, desired)


# ── Harness control: what typed nodes cost ────────────────────────────────────


class TestTypedBaseline:
    """The yardstick: typed nodes cost one scoped read, whatever their number.

    Passes before and after the fix — it proves the harness measures what the
    raw tests claim, and fixes the number raw nodes must match.
    """

    async def test_typed_nodes_cost_one_scoped_read(self, plan: Planner) -> None:
        for count in (SMALL, LARGE):
            observed = await _planned(plan, _typed_design(count))

            assert len(observed.result.unchanged) == 1 + count  # the tenant + its BDs
            assert not observed.result.has_changes
            assert len(observed.scoped_reads) == 1
            assert observed.probed_dns == []


# ── The defect: request count must not grow with the raw node count ───────────


class TestBoundedProbes:
    async def test_present_raw_nodes_cost_one_scoped_read(self, plan: Planner) -> None:
        """All raw objects exist and match: a single read, no per-object probe."""
        for count in (SMALL, LARGE):
            cfg = _design(count)
            assert _raw_count(cfg) == count  # anti-thinning: the design is really raw

            observed = await _planned(plan, cfg)

            assert observed.result.has_changes is False
            assert len(observed.result.unchanged) == 3 + count  # chain + every raw node
            assert observed.probed_dns == []
            assert len(observed.gets) == 1, f"{len(observed.gets)} GETs for {count} raw nodes"

    async def test_request_count_is_independent_of_the_raw_count(self, plan: Planner) -> None:
        """The same design shape at two sizes issues the same number of GETs."""
        small = await _planned(plan, _design(SMALL))
        large = await _planned(plan, _design(LARGE))

        assert len(large.gets) == len(small.gets)

    async def test_two_raw_classes_and_a_typed_pod_cost_one_scoped_read(
        self, plan: Planner
    ) -> None:
        """Several raw classes (numeric and bracketed RNs) share the one read."""
        for count in (SMALL, LARGE):
            cfg = _design(count, count, pod_description="the pod")
            assert _raw_count(cfg) == 2 * count

            observed = await _planned(plan, cfg)

            assert observed.result.has_changes is False
            assert len(observed.result.unchanged) == 3 + 2 * count
            assert len(observed.gets) == 1, f"{len(observed.gets)} GETs for {2 * count} raw nodes"

    async def test_raw_drift_is_found_by_the_read_not_by_probing(self, plan: Planner) -> None:
        """A present object that differs is diffed from the scoped read too."""
        for count in (SMALL, LARGE):
            observed = await _planned(
                plan,
                _design(count, server_descr={2: "drifted"}),
                stored=_design(count),
            )

            assert list(observed.result.updates) == [_server_dn(2)]
            assert observed.probed_dns == []
            assert len(observed.gets) == 1

    async def test_an_absent_raw_object_costs_exactly_its_own_probe(self, plan: Planner) -> None:
        """One missing object among N present ones: one read + one probe, any N."""
        costs: list[int] = []
        for count in (SMALL, LARGE):
            observed = await _planned(
                plan,
                _design(count + 1),  # desires one more server than is stored
                stored=_design(count),
            )

            assert observed.result.creates == [_server_dn(count)]
            assert observed.probed_dns == [_server_dn(count)]
            assert len(observed.scoped_reads) == 1
            costs.append(len(observed.gets))

        assert costs == [2, 2]

    async def test_greenfield_cost_does_not_grow(self, plan: Planner) -> None:
        """Nothing stored: the whole design is a create; cost is flat in the raw count.

        Passes before the fix as well (no read counterpart to mispair) — it
        pins that bounding the matcher does not break the empty-fabric path.
        """
        costs: list[int] = []
        for count in (SMALL, LARGE):
            observed = await plan(FakeApic(), _design(count))

            assert set(observed.result.creates) == _pod_dns(count)
            assert observed.result.updates == {}
            assert observed.result.unchanged == []
            costs.append(len(observed.gets))

        assert costs[0] == costs[1]


# ── Verdicts: unchanged by bounding the count ─────────────────────────────────


class TestVerdictsAreUnchanged:
    """These pass before the fix and must keep passing after it."""

    async def test_everything_present_and_equal_is_a_no_op(self, plan: Planner) -> None:
        observed = await _planned(plan, _design(6, 3, pod_description="the pod"))

        result = observed.result
        assert result.has_changes is False
        assert result.creates == []
        assert result.updates == {}
        assert set(result.unchanged) == _pod_dns(6, 3)
        assert len(result.unchanged) == len(set(result.unchanged))  # each DN booked once

    async def test_one_drifted_raw_attribute_is_exactly_one_update(self, plan: Planner) -> None:
        observed = await _planned(
            plan,
            _design(6, 3, server_descr={2: "wanted"}),
            stored=_design(6, 3),
        )

        result = observed.result
        # (current, desired), under the wire name of the raw channel.
        assert result.updates == {_server_dn(2): {"descr": ("server-2", "wanted")}}
        assert result.creates == []
        assert set(result.unchanged) == _pod_dns(6, 3) - {_server_dn(2)}

    async def test_swapped_values_are_attributed_to_the_right_dns(self, plan: Planner) -> None:
        """Siblings of one class are paired by identity, never by position or class."""
        observed = await _planned(
            plan,
            _design(6, server_descr={1: "server-4", 4: "server-1"}),
            stored=_design(6),
        )

        assert observed.result.updates == {
            _server_dn(1): {"descr": ("server-1", "server-4")},
            _server_dn(4): {"descr": ("server-4", "server-1")},
        }
        assert observed.result.creates == []

    async def test_a_missing_raw_object_is_a_create(self, plan: Planner) -> None:
        observed = await _planned(
            plan, _design(6, 3), stored=_design(6, 3, skip_servers=frozenset({4}))
        )

        result = observed.result
        assert result.creates == [_server_dn(4)]
        assert result.updates == {}
        assert set(result.unchanged) == _pod_dns(6, 3) - {_server_dn(4)}

    async def test_a_missing_raw_object_with_an_absent_raw_parent_chain(
        self, plan: Planner
    ) -> None:
        """Typed parents absent too: the whole branch is created, raw leaves included."""
        stored = controller()  # the domain root only
        observed = await _planned(plan, _design(4, 2), stored=stored)

        assert set(observed.result.creates) == _pod_dns(4, 2) - {"uni/controller"}
        assert observed.result.updates == {}

    async def test_typed_drift_next_to_raw_nodes_is_still_reported(self, plan: Planner) -> None:
        observed = await _planned(
            plan,
            _design(6, 3, pod_description="new"),
            stored=_design(6, 3, pod_description="old"),
        )

        assert observed.result.updates == {POD_DN: {"description": ("old", "new")}}
        assert observed.result.creates == []
        assert set(observed.result.unchanged) == _pod_dns(6, 3) - {POD_DN}

    async def test_drift_in_every_kind_is_reported_together(self, plan: Planner) -> None:
        """Typed drift, raw drift, a raw create and untouched raw neighbours at once."""
        observed = await _planned(
            plan,
            _design(7, 3, server_descr={0: "wanted"}, pod_description="new"),
            stored=_design(7, 3, skip_servers=frozenset({6}), pod_description="old"),
        )

        result = observed.result
        assert result.creates == [_server_dn(6)]
        assert result.updates == {
            POD_DN: {"description": ("old", "new")},
            _server_dn(0): {"descr": ("server-0", "wanted")},
        }
        assert set(result.unchanged) == _pod_dns(7, 3) - {POD_DN, _server_dn(0), _server_dn(6)}

    async def test_a_foreign_raw_object_is_ignored(self, plan: Planner) -> None:
        """The APIC holds servers the design never declares: never reported, never deleted."""
        observed = await _planned(plan, _design(3), stored=_design(9, 2))

        result = observed.result
        assert result.has_changes is False
        assert set(result.unchanged) == _pod_dns(3)

    async def test_a_pushed_design_plans_clean(self, plan: Planner) -> None:
        """Round trip through the fake's write path, not its seed shortcut."""
        cfg = _design(SMALL, SMALL, pod_description="the pod")
        fake = FakeApic()
        with fake.client() as aci:
            cfg.push(aci)

        observed = await plan(fake, cfg)

        assert observed.result.has_changes is False
        assert set(observed.result.unchanged) == _pod_dns(SMALL, SMALL)


# ── The brownfield path ───────────────────────────────────────────────────────


def _imported(fake: FakeApic) -> Cursor:
    """Capture ``uni/controller`` of *fake* and re-express it as a design."""
    with fake.client() as aci:
        captured = snapshot.take(aci, "uni/controller")
    return to_design(captured, on_unknown="raw")


def _edited_after_import() -> tuple[FakeApic, Cursor]:
    """An imported design, then one stored object edited behind its back."""
    fake = FakeApic()
    fake.seed(_design(6, 3).to_payload())
    imported = _imported(fake)
    with fake.client() as aci:
        _design(6, 3, server_descr={3: "edited"}).push(aci)
    return fake, imported


class TestBrownfieldImport:
    async def test_replanning_an_import_is_clean_and_bounded(self, plan: Planner) -> None:
        costs: list[int] = []
        for count in (SMALL, LARGE):
            fake = FakeApic()
            fake.seed(_design(count, count, pod_description="the pod").to_payload())
            imported = _imported(fake)
            assert _raw_count(imported) == 2 * count  # anti-thinning: the import is raw

            observed = await plan(fake, imported)

            assert observed.result.has_changes is False
            assert set(observed.result.unchanged) == _pod_dns(count, count)
            assert observed.probed_dns == []
            assert len(observed.gets) == 1, f"{len(observed.gets)} GETs for {2 * count} raw nodes"
            costs.append(len(observed.gets))

        assert costs[0] == costs[1]

    async def test_drift_after_an_import_lands_on_the_right_raw_node(self, plan: Planner) -> None:
        fake, imported = _edited_after_import()

        observed = await plan(fake, imported)

        assert observed.result.updates == {_server_dn(3): {"descr": ("edited", "server-3")}}
        assert observed.result.creates == []
        assert set(observed.result.unchanged) == _pod_dns(6, 3) - {_server_dn(3)}

    async def test_drift_after_an_import_is_found_without_probing(self, plan: Planner) -> None:
        fake, imported = _edited_after_import()

        observed = await plan(fake, imported)

        assert observed.probed_dns == []
        assert len(observed.gets) == 1
