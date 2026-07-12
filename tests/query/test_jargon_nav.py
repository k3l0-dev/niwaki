"""Tests for jargon navigation integration with Query builders.

Covers:
- Jargon no-arg → returns Query scoped to parent DN
- Jargon with name → returns NiwakiNode (existing behavior, unchanged)
- NiwakiNode.query(cls) → Query scoped to node DN
- AsyncNiwakiNode.query(cls) → AsyncQuery scoped to node DN
- Niwaki.query(cls) → global Query (not list)
- AsyncNiwaki.query(cls) → global AsyncQuery (not coroutine)
- String class name via .query("topSystem")
"""

from __future__ import annotations

from unittest.mock import MagicMock

from niwaki.facade import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki.query import AsyncQuery, Query

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_niwaki() -> Niwaki:
    """Return a Niwaki instance with a mocked sync session."""
    aci = object.__new__(Niwaki)
    aci._session = MagicMock()  # type: ignore[reportPrivateUsage]
    return aci


def _make_async_niwaki() -> AsyncNiwaki:
    """Return an AsyncNiwaki with a mocked async session."""
    aci = object.__new__(AsyncNiwaki)
    session = MagicMock()
    aci._session = session  # type: ignore[reportPrivateUsage]
    return aci


# ── Niwaki.query() ────────────────────────────────────────────────────────────


class TestNiwakiQuery:
    def test_returns_query_not_list(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        aci = _make_niwaki()
        result = aci.query(fvTenant)
        assert isinstance(result, Query)

    def test_cls_name_set(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        aci = _make_niwaki()
        q = aci.query(fvBD)
        path, _ = q.build()
        assert path == "/api/class/fvBD.json"

    def test_global_query_no_scope_dn(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        aci = _make_niwaki()
        q = aci.query(fvBD)
        path, _ = q.build()
        assert path.startswith("/api/class/")

    def test_string_cls_name(self) -> None:
        aci = _make_niwaki()
        q = aci.query("topSystem")
        assert isinstance(q, Query)
        path, _ = q.build()
        assert path == "/api/class/topSystem.json"


# ── AsyncNiwaki.query() ───────────────────────────────────────────────────────


class TestAsyncNiwakiQuery:
    def test_returns_async_query_not_coroutine(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        aci = _make_async_niwaki()
        result = aci.query(fvTenant)
        # Must NOT be a coroutine — it's a builder
        import inspect

        assert not inspect.iscoroutine(result)
        assert isinstance(result, AsyncQuery)

    def test_cls_name_set(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        aci = _make_async_niwaki()
        path, _ = aci.query(fvBD).build()
        assert path == "/api/class/fvBD.json"

    def test_global_query_no_scope_dn(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        aci = _make_async_niwaki()
        path, _ = aci.query(fvBD).build()
        assert path.startswith("/api/class/")

    def test_string_cls_name(self) -> None:
        aci = _make_async_niwaki()
        q = aci.query("topSystem")
        assert isinstance(q, AsyncQuery)
        path, _ = q.build()
        assert path == "/api/class/topSystem.json"


# ── NiwakiNode.query(cls) ─────────────────────────────────────────────────────


class TestNiwakiNodeQuery:
    def test_returns_query_scoped_to_node_dn(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvTenant import fvTenant

        aci = _make_niwaki()
        node = aci.root.mo(fvTenant, name="prod")
        q = node.query(fvBD)
        assert isinstance(q, Query)
        path, params = q.build()
        assert path == "/api/mo/uni/tn-prod.json"
        assert params["target-subtree-class"] == "fvBD"

    def test_string_cls(self) -> None:
        aci = _make_niwaki()
        node = aci.root
        q = node.query("topSystem")
        assert isinstance(q, Query)
        _, params = q.build()
        assert params["target-subtree-class"] == "topSystem"


# ── AsyncNiwakiNode.query(cls) ────────────────────────────────────────────────


class TestAsyncNiwakiNodeQuery:
    def test_returns_async_query_scoped_to_dn(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvTenant import fvTenant

        aci = _make_async_niwaki()
        node = aci.root.mo(fvTenant, name="prod")
        q = node.query(fvBD)
        assert isinstance(q, AsyncQuery)
        path, params = q.build()
        assert path == "/api/mo/uni/tn-prod.json"
        assert params["target-subtree-class"] == "fvBD"


# ── Jargon no-arg navigation ──────────────────────────────────────────────────


class TestJargonNoArgQuery:
    def test_bd_no_arg_returns_query(self) -> None:
        aci = _make_niwaki()
        result = aci.root.tenant("prod").bd()
        assert isinstance(result, Query)

    def test_bd_no_arg_scope_dn_is_tenant_dn(self) -> None:
        aci = _make_niwaki()
        q = aci.root.tenant("prod").bd()
        path, _ = q.build()
        assert path == "/api/mo/uni/tn-prod.json"

    def test_bd_no_arg_cls_name_is_fvBD(self) -> None:
        aci = _make_niwaki()
        q = aci.root.tenant("prod").bd()
        _, params = q.build()
        assert params["target-subtree-class"] == "fvBD"

    def test_bd_with_name_returns_niwaki_node(self) -> None:
        aci = _make_niwaki()
        result = aci.root.tenant("prod").bd("web")
        assert isinstance(result, NiwakiNode)

    def test_bd_with_name_dn_is_correct(self) -> None:
        aci = _make_niwaki()
        node = aci.root.tenant("prod").bd("web")
        assert node.dn == "uni/tn-prod/BD-web"

    def test_epg_no_arg_returns_query(self) -> None:
        aci = _make_niwaki()
        # ap → fvAp; epg → fvAEPg (both in CHILD_MAP)
        result = aci.root.tenant("prod").app("app").epg()
        assert isinstance(result, Query)

    def test_subnet_no_arg_returns_query(self) -> None:
        aci = _make_niwaki()
        result = aci.root.tenant("prod").bd("web").subnet()
        assert isinstance(result, Query)

    def test_subnet_no_arg_scope_dn_is_bd_dn(self) -> None:
        aci = _make_niwaki()
        q = aci.root.tenant("prod").bd("web").subnet()
        path, _ = q.build()
        assert path == "/api/mo/uni/tn-prod/BD-web.json"

    def test_query_builder_url(self) -> None:
        """End-to-end: jargon no-arg → correct APIC URL path and params."""
        aci = _make_niwaki()
        q = aci.root.tenant("prod").bd()
        path, params = q.build()
        assert path == "/api/mo/uni/tn-prod.json"
        assert params["query-target"] == "subtree"
        assert params["target-subtree-class"] == "fvBD"

    def test_filter_chained_on_jargon_query(self) -> None:
        aci = _make_niwaki()
        q = aci.root.tenant("prod").bd().where(name="web")
        _, params = q.build()
        assert "query-target-filter" in params
        assert 'eq(fvBD.name,"web")' in params["query-target-filter"]


# ── Async jargon no-arg ───────────────────────────────────────────────────────


class TestAsyncJargonNoArgQuery:
    def test_bd_no_arg_returns_async_query(self) -> None:
        aci = _make_async_niwaki()
        result = aci.root.tenant("prod").bd()
        assert isinstance(result, AsyncQuery)

    def test_bd_no_arg_scope_dn(self) -> None:
        aci = _make_async_niwaki()
        q = aci.root.tenant("prod").bd()
        path, _ = q.build()
        assert path == "/api/mo/uni/tn-prod.json"

    def test_bd_with_name_returns_async_node(self) -> None:
        aci = _make_async_niwaki()
        result = aci.root.tenant("prod").bd("web")
        assert isinstance(result, AsyncNiwakiNode)
