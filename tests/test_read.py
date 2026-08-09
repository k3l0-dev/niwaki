"""The sharded flat reader — R-3-safe bulk reads, tree rebuilt by DN.

Three layers, each tested on its own: sharding (pure, budget arithmetic),
tree rebuilding (pure, DN attachment and orphan policy), and the reader that
drives a session shard by shard.  The session is faked at the ``get`` seam —
the reader's contract is the request shape, not HTTP.
"""

from __future__ import annotations

from typing import Any

import pytest

from niwaki._read import build_tree, read_subtree, read_subtree_async, shard_classes
from niwaki.exceptions import DeserializationError, NiwakiError

# ``from_apic`` dispatches through the model REGISTRY, which fills lazily as
# generated classes are imported.  In the real plan path the desired tree has
# instantiated every declared class before the read runs; these imports state
# the same precondition for the tests.
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.fv.fvCtx import fvCtx  # noqa: F401
from niwaki.models.fv.fvSubnet import fvSubnet  # noqa: F401
from niwaki.models.fv.fvTenant import fvTenant  # noqa: F401


def _flat(cls: str, dn: str, **attrs: str) -> dict[str, Any]:
    """One flat-response envelope, as the APIC returns for class queries."""
    return {cls: {"attributes": {"dn": dn, **attrs}}}


class TestShardClasses:
    def test_a_small_set_is_one_shard(self) -> None:
        assert shard_classes(["fvBD", "fvTenant", "fvCtx"]) == ["fvBD,fvCtx,fvTenant"]

    def test_input_order_and_duplicates_do_not_matter(self) -> None:
        assert shard_classes(["fvCtx", "fvBD", "fvCtx"]) == shard_classes(["fvBD", "fvCtx"])

    def test_every_shard_respects_the_budget(self) -> None:
        names = [f"class{i:04d}" for i in range(200)]  # 9 chars each
        shards = shard_classes(names, budget=100)
        assert len(shards) > 1
        encoded = [shard.replace(",", "%2C") for shard in shards]
        assert all(len(e) <= 100 for e in encoded)

    def test_nothing_is_lost_or_invented_by_sharding(self) -> None:
        names = {f"cls{i}" for i in range(50)}
        shards = shard_classes(names, budget=40)
        assert {name for shard in shards for name in shard.split(",")} == names

    def test_shards_preserve_sorted_order_across_boundaries(self) -> None:
        # Each separator costs 3 bytes on the wire (%2C): "a,b" = 1+3+1 = 5.
        shards = shard_classes(["a", "b", "c", "d"], budget=5)
        assert shards == ["a,b", "c,d"]

    def test_the_budget_is_the_encoded_size_not_the_raw_size(self) -> None:
        """httpx sends ``,`` as ``%2C`` — the budget must count 3 per comma.

        Raw "a,b,c" is 5 bytes but rides as "a%2Cb%2Cc" (9): with budget=8 the
        three names must not share one shard, though their raw join would fit.
        """
        assert shard_classes(["a", "b", "c"], budget=8) == ["a,b", "c"]

    def test_a_name_longer_than_the_budget_ships_alone(self) -> None:
        shards = shard_classes(["short", "averyveryverylongclassname"], budget=10)
        assert "averyveryverylongclassname" in shards

    def test_empty_input_yields_no_shards(self) -> None:
        assert shard_classes([]) == []


class TestBuildTree:
    def test_rebuilds_a_nested_hierarchy_from_flat_items(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web"),
            _flat("fvSubnet", "uni/tn-p/BD-web/subnet-[10.0.1.1/24]", ip="10.0.1.1/24"),
        ]
        root = build_tree(items, "uni/tn-p")
        assert root is not None
        (bd,) = root.children
        assert bd.rn == "BD-web"
        (subnet,) = bd.children
        assert subnet.rn == "subnet-[10.0.1.1/24]"

    def test_item_order_is_irrelevant(self) -> None:
        items = [
            _flat("fvSubnet", "uni/tn-p/BD-web/subnet-[10.0.1.1/24]", ip="10.0.1.1/24"),
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web"),
        ]
        root = build_tree(items, "uni/tn-p")
        assert root is not None
        assert root.children[0].children[0].rn == "subnet-[10.0.1.1/24]"

    def test_a_missing_root_returns_none(self) -> None:
        assert build_tree([], "uni/tn-p") is None
        # Descendants without the root are meaningless fragments — still None.
        assert build_tree([_flat("fvBD", "uni/tn-p/BD-w", name="w")], "uni/tn-p") is None

    def test_a_foreign_position_is_dropped(self) -> None:
        """A requested class at a position whose ancestors were not requested.

        A subnet under an EPG when only BD subnets were declared: its parent DN
        is not in the result set, so the fragment must not attach anywhere —
        and in particular not to the root.
        """
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web"),
            _flat("fvSubnet", "uni/tn-p/ap-a/epg-e/subnet-[10.9.9.9/32]", ip="10.9.9.9/32"),
        ]
        root = build_tree(items, "uni/tn-p")
        assert root is not None
        assert [child.rn for child in root.children] == ["BD-web"]
        assert root.children[0].children == []

    def test_a_bracketed_dn_with_slashes_attaches_to_the_right_parent(self) -> None:
        """The parent of ``subnet-[10.0.1.1/24]`` is the BD, not ``...1.1``."""
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web"),
            _flat("fvSubnet", "uni/tn-p/BD-web/subnet-[10.0.1.1/24]", ip="10.0.1.1/24"),
        ]
        root = build_tree(items, "uni/tn-p")
        assert root is not None
        assert root.children[0].children[0].rn == "subnet-[10.0.1.1/24]"

    def test_a_duplicate_dn_keeps_the_last_occurrence(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web", unicastRoute="no"),
            _flat("fvBD", "uni/tn-p/BD-web", name="web", unicastRoute="yes"),
        ]
        root = build_tree(items, "uni/tn-p")
        assert root is not None
        (bd,) = root.children
        assert bd["unicastRoute"] is True  # the second item's "yes", coerced

    def test_an_item_without_a_dn_fails_loud_and_typed(self) -> None:
        """Typed, so a caller's ``except NiwakiError`` catches it like any
        other failure of the same read (the errors-guide promise)."""
        with pytest.raises(DeserializationError, match="no dn"):
            build_tree([{"fvBD": {"attributes": {"name": "web"}}}], "uni/tn-p")
        assert issubclass(DeserializationError, NiwakiError)

    def test_a_malformed_envelope_fails_loud_and_typed(self) -> None:
        two_keys = {"fvBD": {"attributes": {"dn": "x"}}, "fvCtx": {"attributes": {"dn": "y"}}}
        with pytest.raises(DeserializationError, match="one-key envelope"):
            build_tree([two_keys], "uni/tn-p")

    def test_items_deserialise_to_typed_models(self) -> None:
        root = build_tree(
            [_flat("fvTenant", "uni/tn-p", name="p"), _flat("fvBD", "uni/tn-p/BD-w", name="w")],
            "uni/tn-p",
        )
        assert root is not None
        assert isinstance(root.children[0], fvBD)


class _FakeSession:
    """Records every paginated GET and serves canned flat items, per shard.

    Fakes the transport's ``_get_all_pages`` — the auto-paginating seam the
    reader drives (the same one the query builder uses).
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.requests: list[tuple[str, dict[str, Any], int]] = []

    def _get_all_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> list[dict[str, Any]]:
        self.requests.append((path, dict(params), page_size))
        allowed = set(params["target-subtree-class"].split(","))
        return [item for item in self._items if next(iter(item)) in allowed]


class _FakeAsyncSession(_FakeSession):
    async def _get_all_pages(  # type: ignore[override]
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> list[dict[str, Any]]:
        return _FakeSession._get_all_pages(self, path, params, page_size=page_size)


_ITEMS = [
    _flat("fvTenant", "uni/tn-p", name="p"),
    _flat("fvBD", "uni/tn-p/BD-web", name="web"),
    _flat("fvCtx", "uni/tn-p/ctx-main", name="main"),
]


class TestReadSubtree:
    def test_a_small_class_set_is_one_request(self) -> None:
        session = _FakeSession(_ITEMS)
        root = read_subtree(session, "uni/tn-p", ["fvTenant", "fvBD", "fvCtx"])
        assert root is not None
        assert len(session.requests) == 1
        path, params, page_size = session.requests[0]
        assert path == "/api/mo/uni/tn-p.json"
        assert params == {"query-target": "subtree", "target-subtree-class": "fvBD,fvCtx,fvTenant"}
        assert page_size == 500  # the response side is paginated, not hoped small
        assert {child.rn for child in root.children} == {"BD-web", "ctx-main"}

    def test_a_large_class_set_shards_and_merges(self) -> None:
        session = _FakeSession(_ITEMS)
        root = read_subtree(session, "uni/tn-p", ["fvTenant", "fvBD", "fvCtx"], budget=8)
        assert root is not None
        assert len(session.requests) == 3
        assert all(len(p["target-subtree-class"]) <= 8 for _, p, _size in session.requests)
        # The tree is whole even though every class came from a different request.
        assert {child.rn for child in root.children} == {"BD-web", "ctx-main"}

    def test_an_absent_scope_returns_none(self) -> None:
        session = _FakeSession([])
        assert read_subtree(session, "uni/tn-p", ["fvTenant"]) is None

    async def test_the_async_twin_issues_the_same_requests(self) -> None:
        sync_session = _FakeSession(_ITEMS)
        async_session = _FakeAsyncSession(_ITEMS)
        sync_root = read_subtree(sync_session, "uni/tn-p", ["fvTenant", "fvBD"], budget=12)
        async_root = await read_subtree_async(
            async_session, "uni/tn-p", ["fvTenant", "fvBD"], budget=12
        )
        assert async_session.requests == sync_session.requests
        assert sync_root is not None and async_root is not None
        assert [c.rn for c in async_root.children] == [c.rn for c in sync_root.children]
