"""Tests for Query (sync) builder — URL/param construction and execution.

Covers:
- _build_path_and_params() for all accumulator states
- Immutable builder pattern (chaining does not mutate original)
- fetch() / first() / count() / stream() execution against mocked sessions
- String-based class name query
- page_size validation
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from niwaki.models.base import ManagedObject
from niwaki.query import Query, wcard

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_session(**kwargs: Any) -> MagicMock:
    """Return a mock ApicSession with configurable return values."""
    session = MagicMock()
    session._get_all_pages.return_value = kwargs.get("raw_items", [])
    session._get_imdata.return_value = kwargs.get("raw_items", [])
    session._iter_pages.return_value = iter(kwargs.get("pages", []))
    session._request_checked.return_value.json.return_value = kwargs.get(
        "count_response", {"totalCount": "0", "imdata": []}
    )
    return session


def _fvTenant_item(name: str) -> dict:
    return {"fvTenant": {"attributes": {"name": name}}}


def _fvBD_item(name: str) -> dict:
    return {"fvBD": {"attributes": {"name": name}}}


# ── URL / param construction ──────────────────────────────────────────────────


class TestBuildPathAndParams:
    def test_global_class_query(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        q: Query[fvTenant] = Query(fvTenant, MagicMock())
        path, params = q.build()
        assert path == "/api/class/fvTenant.json"
        assert params == {}

    def test_string_cls_global_query(self) -> None:
        q: Query[ManagedObject] = Query("topSystem", MagicMock())
        path, params = q.build()
        assert path == "/api/class/topSystem.json"
        assert params == {}

    def test_scoped_query_adds_subtree_params(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod")
        path, params = q.build()
        assert path == "/api/mo/uni/tn-prod.json"
        assert params["query-target"] == "subtree"
        assert params["target-subtree-class"] == "fvBD"

    def test_children_scope(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").children()
        _, params = q.build()
        assert params["query-target"] == "children"
        assert params["target-subtree-class"] == "fvBD"

    def test_where_kwargs_adds_filter(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).where(name="web")
        _, params = q.build()
        assert "query-target-filter" in params
        assert 'eq(fvBD.name,"web")' in params["query-target-filter"]

    def test_where_bool_coerced(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).where(arpFlood=True)
        _, params = q.build()
        assert 'eq(fvBD.arpFlood,"yes")' in params["query-target-filter"]

    def test_where_multiple_kwargs_and_combined(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).where(name="web", arpFlood=True)
        _, params = q.build()
        assert params["query-target-filter"].startswith("and(")

    def test_where_chained_and_combined(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).where(name="web").where(arpFlood=True)
        _, params = q.build()
        assert params["query-target-filter"].startswith("and(")

    def test_where_explicit_filter_expr(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).where(wcard("name", "prod-*"))
        _, params = q.build()
        assert 'wcard(name,"prod-*")' in params["query-target-filter"]

    def test_with_faults_adds_rsp_subtree_include(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_faults()
        _, params = q.build()
        assert "faults" in params["rsp-subtree-include"]
        assert "required" in params["rsp-subtree-include"]

    def test_with_health(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_health()
        _, params = q.build()
        assert "health" in params["rsp-subtree-include"]

    def test_with_stats(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_stats()
        _, params = q.build()
        assert "stats" in params["rsp-subtree-include"]

    def test_with_relations(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_relations()
        _, params = q.build()
        assert "relations" in params["rsp-subtree-include"]

    def test_include_sets_rsp_subtree_children(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet)
        _, params = q.build()
        assert params["rsp-subtree"] == "children"
        assert "fvSubnet" in params["rsp-subtree-class"]

    def test_include_string_class(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).include("fvSubnet")
        _, params = q.build()
        assert "fvSubnet" in params["rsp-subtree-class"]

    def test_config_only(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).config_only()
        _, params = q.build()
        assert params["rsp-prop-include"] == "config-only"

    def test_naming_only(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).naming_only()
        _, params = q.build()
        assert params["rsp-prop-include"] == "naming-only"

    def test_order_by_asc(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).order_by("name")
        _, params = q.build()
        assert params["order-by"] == "fvBD.name|asc"

    def test_order_by_desc(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).order_by("name", desc=True)
        _, params = q.build()
        assert params["order-by"] == "fvBD.name|desc"

    def test_order_by_already_qualified(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).order_by("fvBD.name")
        _, params = q.build()
        assert params["order-by"] == "fvBD.name|asc"

    def test_no_rsp_prop_include_when_all(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock())
        _, params = q.build()
        assert "rsp-prop-include" not in params

    def test_subtree_where_sets_rsp_subtree_filter(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet).subtree_where(wcard("fvSubnet.ip", "10.*"))
        _, params = q.build()
        assert "rsp-subtree-filter" in params
        assert 'wcard(fvSubnet.ip,"10.*")' in params["rsp-subtree-filter"]

    def test_subtree_where_kwargs_auto_qualified(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet).subtree_where(ip="10.0.0.1/24")
        _, params = q.build()
        assert 'eq(fvBD.ip,"10.0.0.1/24")' in params["rsp-subtree-filter"]

    def test_subtree_where_chained_and_combined(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet
        from niwaki.query import eq

        q = (
            Query(fvBD, MagicMock())
            .include(fvSubnet)
            .subtree_where(eq("fvSubnet.ip", "10.0.0.1/24"))
            .subtree_where(eq("fvSubnet.scope", "public"))
        )
        _, params = q.build()
        assert params["rsp-subtree-filter"].startswith("and(")

    def test_subtree_where_no_args_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock())
        with pytest.raises(ValueError, match="at least one filter"):
            q.subtree_where()

    def test_subtree_where_absent_when_not_called(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock())
        _, params = q.build()
        assert "rsp-subtree-filter" not in params


# ── Immutable builder ─────────────────────────────────────────────────────────


class TestImmutableBuilder:
    def test_where_does_not_mutate_original(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        original = Query(fvBD, MagicMock())
        filtered = original.where(name="web")
        _, orig_params = original.build()
        _, filt_params = filtered.build()
        assert "query-target-filter" not in orig_params
        assert "query-target-filter" in filt_params

    def test_chained_copies_are_independent(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        base = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod")
        faulted = base.with_faults()
        healthy = base.with_health()
        _, fp = faulted.build()
        _, hp = healthy.build()
        assert "faults" in fp.get("rsp-subtree-include", "")
        assert "faults" not in hp.get("rsp-subtree-include", "")
        assert "health" in hp.get("rsp-subtree-include", "")

    def test_under_does_not_mutate(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock())
        scoped = q.under("uni/tn-prod")
        # Original is still a global class query; scoped is a MO-scoped query.
        orig_path, _ = q.build()
        scoped_path, _ = scoped.build()
        assert orig_path == "/api/class/fvBD.json"
        assert scoped_path == "/api/mo/uni/tn-prod.json"


# ── page_size validation ──────────────────────────────────────────────────────


class TestPageSize:
    def test_positive_page_size(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session()
        Query(fvBD, session).page_size(100).fetch()
        call_kwargs = session._get_all_pages.call_args.kwargs
        assert call_kwargs["page_size"] == 100

    def test_zero_page_size_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValueError, match="page_size must be > 0"):
            Query(fvBD, MagicMock()).page_size(0)

    def test_negative_page_size_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValueError):
            Query(fvBD, MagicMock()).page_size(-1)


# ── Execution ─────────────────────────────────────────────────────────────────


class TestFetch:
    def test_fetch_returns_typed_list(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        raw = [_fvTenant_item("prod"), _fvTenant_item("dev")]
        session = _make_session(raw_items=raw)
        result = Query(fvTenant, session).fetch()
        assert len(result) == 2
        assert all(isinstance(t, fvTenant) for t in result)

    def test_fetch_empty(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(raw_items=[])
        result = Query(fvBD, session).fetch()
        assert result == []

    def test_fetch_calls_get_all_pages_with_correct_path(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session()
        Query(fvTenant, session).fetch()
        session._get_all_pages.assert_called_once()
        call_args = session._get_all_pages.call_args
        assert call_args[0][0] == "/api/class/fvTenant.json"

    def test_fetch_scoped_uses_mo_path(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session()
        Query(fvBD, session, scope_dn="uni/tn-prod").fetch()
        call_path = session._get_all_pages.call_args[0][0]
        assert call_path == "/api/mo/uni/tn-prod.json"


class TestFirst:
    def test_first_returns_single_object(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        raw = [_fvTenant_item("prod")]
        session = _make_session(raw_items=raw)
        result = Query(fvTenant, session).first()
        assert isinstance(result, fvTenant)
        assert result.name == "prod"

    def test_first_empty_returns_none(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(raw_items=[])
        assert Query(fvBD, session).first() is None

    def test_first_uses_page_size_1(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session()
        Query(fvTenant, session).first()
        call_params = session._get_imdata.call_args[0][1]
        assert call_params["page"] == "0"
        assert call_params["page-size"] == "1"


class TestCount:
    def test_count_returns_integer(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "42", "imdata": []})
        n = Query(fvBD, session).count()
        assert n == 42

    def test_count_zero(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "0", "imdata": []})
        assert Query(fvBD, session).count() == 0

    def test_count_adds_count_only_param(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "5", "imdata": []})
        Query(fvBD, session).count()
        call_params = session._request_checked.call_args[0][1]
        assert call_params["page-size"] == "1"  # count = totalCount of a 1-object page


class TestStream:
    def test_stream_yields_objects(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        page1 = [_fvTenant_item("prod"), _fvTenant_item("dev")]
        page2 = [_fvTenant_item("infra")]
        session = _make_session(pages=[page1, page2])
        results = list(Query(fvTenant, session).stream())
        assert len(results) == 3
        assert all(isinstance(t, fvTenant) for t in results)

    def test_stream_empty_yields_nothing(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(pages=[])
        results = list(Query(fvBD, session).stream())
        assert results == []


# ── String-based (unregistered classes) ──────────────────────────────────────


class TestStringClassQuery:
    def test_string_cls_produces_correct_path(self) -> None:
        q = Query("topSystem", MagicMock())
        path, _ = q.build()
        assert path == "/api/class/topSystem.json"

    def test_string_cls_scoped(self) -> None:
        q = Query("topSystem", MagicMock(), scope_dn="uni")
        path, params = q.build()
        assert path == "/api/mo/uni.json"
        assert params["target-subtree-class"] == "topSystem"

    def test_fetch_unregistered_returns_managed_object(self) -> None:
        raw = [{"topSystem": {"attributes": {"dn": "topology/pod-1/node-101", "role": "leaf"}}}]
        session = _make_session(raw_items=raw)
        results = Query("topSystem", session).fetch()
        assert len(results) == 1
        # APIC attrs go into model_extra for unregistered classes
        assert isinstance(results[0], ManagedObject)


# ── Accumulator edge cases ────────────────────────────────────────────────────


class TestAccumulatorEdgeCases:
    def test_subtree_restores_default_after_children(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").children().subtree()
        _, params = q.build()
        assert params["query-target"] == "subtree"

    def test_where_without_arguments_is_a_no_op(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock())
        assert q.where() is q
