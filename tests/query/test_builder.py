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
from niwaki.query import FilterValue, Query, wcard

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

    def test_with_faults_embeds_without_required(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_faults()
        _, params = q.build()
        assert params["rsp-subtree-include"] == "faults"
        assert "required" not in params["rsp-subtree-include"]

    def test_only_faulted_adds_required(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).with_faults().only_faulted()
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

    def test_order_by_multi_key(self) -> None:
        q = Query("faultInst", MagicMock()).order_by("severity", desc=True).order_by("code")
        _, params = q.build()
        assert params["order-by"] == "faultInst.severity|desc,faultInst.code|asc"

    def test_self_only_sets_query_target(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").self_only()
        _, params = q.build()
        assert params["query-target"] == "self"
        # query-target=self returns the MO itself — no subtree-class scoping.
        assert "target-subtree-class" not in params

    def test_also_adds_target_subtree_classes(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").also("fvSubnet")
        _, params = q.build()
        assert params["target-subtree-class"] == "fvBD,fvSubnet"

    def test_also_dedupes_and_skips_own_class(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").also("fvBD", "fvSubnet", "fvSubnet")
        _, params = q.build()
        assert params["target-subtree-class"] == "fvBD,fvSubnet"

    def test_also_on_global_query_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        # also() only makes sense on a DN-scoped subtree/children query — on a
        # global class query it would silently narrow, so build() fails loud.
        q = Query(fvBD, MagicMock()).also("fvSubnet")
        with pytest.raises(ValueError, match=r"also\(\)"):
            q.build()

    def test_also_with_self_only_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock(), scope_dn="uni/tn-prod").self_only().also("fvSubnet")
        with pytest.raises(ValueError, match=r"also\(\)"):
            q.build()

    def test_subtree_full(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        q = Query(fvBD, MagicMock()).subtree_full()
        _, params = q.build()
        assert params["rsp-subtree"] == "full"

    def test_subtree_full_overrides_include_depth_keeps_class(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet).subtree_full()
        _, params = q.build()
        assert params["rsp-subtree"] == "full"  # full depth wins over children
        assert params["rsp-subtree-class"] == "fvSubnet"  # the class filter persists

    def test_include_subtree_records(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import SubtreeInclude

        q = Query(fvBD, MagicMock()).include_subtree(
            SubtreeInclude.FAULT_RECORDS, SubtreeInclude.AUDIT_LOGS
        )
        _, params = q.build()
        assert "fault-records" in params["rsp-subtree-include"]
        assert "audit-logs" in params["rsp-subtree-include"]

    def test_include_subtree_dedupes_with_sugar(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import SubtreeInclude

        q = Query(fvBD, MagicMock()).with_faults().include_subtree(SubtreeInclude.FAULTS)
        _, params = q.build()
        assert params["rsp-subtree-include"] == "faults"

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

        # The keyword qualifies with the INCLUDED subtree class (fvSubnet), not
        # the query class (fvBD) — verified live: qualifying with fvBD makes the
        # APIC reject the filter (HTTP 301).
        q = Query(fvBD, MagicMock()).include(fvSubnet).subtree_where(ip="10.0.0.1/24")
        _, params = q.build()
        assert 'eq(fvSubnet.ip,"10.0.0.1/24")' in params["rsp-subtree-filter"]

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

    def test_subtree_where_kwargs_without_include_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        # No include() class → the keyword can't be qualified — fail loud.
        q = Query(fvBD, MagicMock()).with_faults()
        with pytest.raises(ValueError, match="include"):
            q.subtree_where(severity="critical")

    def test_subtree_where_kwargs_ambiguous_multiple_include_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvRsCtx import fvRsCtx
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet, fvRsCtx)
        with pytest.raises(ValueError, match="several"):
            q.subtree_where(scope="public")

    def test_subtree_where_positional_expr_needs_no_include(self) -> None:
        # An explicitly-qualified positional expression carries its own class.
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import eq

        q = (
            Query(fvBD, MagicMock())
            .with_faults()
            .subtree_where(eq("faultInst.severity", "critical"))
        )
        _, params = q.build()
        assert 'eq(faultInst.severity,"critical")' in params["rsp-subtree-filter"]


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


# ── Iteration, slicing, limit ─────────────────────────────────────────────────


class TestIterAndSlice:
    def test_iter_streams_objects(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(pages=[[_fvTenant_item("a"), _fvTenant_item("b")]])
        assert [t.name for t in Query(fvTenant, session)] == ["a", "b"]

    def test_list_of_query(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(pages=[[_fvTenant_item("a")]])
        assert len(list(Query(fvTenant, session))) == 1

    def test_slice_is_a_lazy_new_query(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        base = Query(fvBD, MagicMock())
        limited = base[:10]
        assert limited is not base
        assert base._limit is None
        assert limited._limit == 10

    def test_slice_limits_results(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        page = [_fvTenant_item(str(i)) for i in range(5)]
        session = _make_session(pages=[page])
        assert len(list(Query(fvTenant, session)[:2])) == 2

    def test_slice_caps_page_size_server_side(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(pages=[[_fvTenant_item("a")]])
        list(Query(fvTenant, session)[:3])
        assert session._iter_pages.call_args.kwargs["page_size"] == 3

    def test_fetch_honors_limit(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        page = [_fvTenant_item(str(i)) for i in range(5)]
        session = _make_session(pages=[page])
        assert len(Query(fvTenant, session)[:2].fetch()) == 2

    def test_slice_zero_yields_nothing_without_request(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(pages=[[_fvTenant_item("a")]])
        assert list(Query(fvBD, session)[:0]) == []
        session._iter_pages.assert_not_called()

    def test_full_slice_is_unlimited_copy(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        assert Query(fvBD, MagicMock())[:]._limit is None

    def test_slice_rejects_step(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValueError, match="step"):
            _ = Query(fvBD, MagicMock())[::2]

    def test_slice_rejects_nonzero_start(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValueError, match="offset"):
            _ = Query(fvBD, MagicMock())[5:10]

    def test_slice_rejects_negative_stop(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValueError, match="non-negative"):
            _ = Query(fvBD, MagicMock())[:-1]

    def test_integer_index_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(TypeError, match="sliced, not indexed"):
            _ = Query(fvBD, MagicMock())[0]  # type: ignore[index]


class TestOne:
    def test_one_returns_single_object(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(raw_items=[_fvTenant_item("prod")])
        result = Query(fvTenant, session).one()
        assert isinstance(result, fvTenant)
        assert result.name == "prod"

    def test_one_no_result_raises(self) -> None:
        from niwaki.exceptions import NoResultError
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(raw_items=[])
        with pytest.raises(NoResultError):
            Query(fvBD, session).one()

    def test_one_multiple_raises(self) -> None:
        from niwaki.exceptions import MultipleResultsError
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(raw_items=[_fvTenant_item("a"), _fvTenant_item("b")])
        with pytest.raises(MultipleResultsError):
            Query(fvTenant, session).one()

    def test_one_requests_page_size_2(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(raw_items=[_fvTenant_item("prod")])
        Query(fvTenant, session).one()
        assert session._get_imdata.call_args[0][1]["page-size"] == "2"


class TestExists:
    def test_exists_true(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "3", "imdata": []})
        assert Query(fvBD, session).exists() is True

    def test_exists_false(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "0", "imdata": []})
        assert Query(fvBD, session).exists() is False


class TestExecuteRaw:
    def test_execute_raw_parses_and_paginates(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        session = _make_session(raw_items=[_fvTenant_item("prod")])
        results = Query(fvTenant, session).execute_raw("/api/class/fvTenant.json", {"x": "y"})
        assert len(results) == 1
        assert results[0]["name"] == "prod"  # uniform wire access (base ManagedObject)
        session._get_all_pages.assert_called_once()


# ── Smart keyword arguments (the value chooses the operator) ───────────────────


class TestSmartKwargs:
    def _filter(self, **kwargs: FilterValue) -> str:
        from niwaki.models._generated.fv.fvBD import fvBD

        _, params = Query(fvBD, MagicMock()).where(**kwargs).build()
        return params["query-target-filter"]

    def test_scalar_is_equality(self) -> None:
        assert self._filter(name="web") == 'eq(fvBD.name,"web")'

    def test_bool_is_coerced_equality(self) -> None:
        assert self._filter(arpFlood=True) == 'eq(fvBD.arpFlood,"yes")'

    def test_list_is_membership_or(self) -> None:
        assert self._filter(name=["a", "b"]) == 'or(eq(fvBD.name,"a"),eq(fvBD.name,"b"))'

    def test_single_element_list_is_eq(self) -> None:
        assert self._filter(name=["a"]) == 'eq(fvBD.name,"a")'

    def test_tuple_is_membership(self) -> None:
        assert self._filter(name=("a", "b")).startswith("or(")

    def test_glob_string_is_wildcard(self) -> None:
        assert self._filter(name="prod-*") == 'wcard(fvBD.name,"prod-*")'

    def test_set_stays_bitmask_equality(self) -> None:
        # REGRESSION GUARD (audit "angle mort E"): a set is a Flags bitmask and
        # must stay an eq(), never become a membership OR.
        assert self._filter(scope={"shared", "public"}) == 'eq(fvBD.scope,"public,shared")'

    def test_any_of_wrapper(self) -> None:
        from niwaki.query import any_of

        assert self._filter(code=any_of("A", "B")) == 'or(eq(fvBD.code,"A"),eq(fvBD.code,"B"))'

    def test_like_wrapper(self) -> None:
        from niwaki.query import like

        assert self._filter(name=like("prod-*")) == 'wcard(fvBD.name,"prod-*")'

    def test_between_wrapper(self) -> None:
        from niwaki.query import between

        assert self._filter(pri=between("1", "5")) == 'bw(fvBD.pri,"1","5")'

    def test_subtree_where_shares_the_dispatch(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvBD, MagicMock()).include(fvSubnet).subtree_where(scope=["a", "b"])
        _, params = q.build()
        assert params["rsp-subtree-filter"].startswith("or(")

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="empty collection"):
            self._filter(name=[])

    def test_none_value_raises(self) -> None:
        with pytest.raises(ValueError, match="None is not a valid"):
            self._filter(name=None)  # type: ignore[arg-type]

    def test_none_in_list_raises(self) -> None:
        with pytest.raises(ValueError, match="None is not a valid"):
            self._filter(name=["a", None])  # type: ignore[list-item]

    def test_filterexpr_as_value_raises(self) -> None:
        from niwaki.query import eq

        with pytest.raises(TypeError, match="positionally"):
            self._filter(name=eq("x", "y"))  # type: ignore[arg-type]

    def test_set_of_enum_members_stays_bitmask(self) -> None:
        from niwaki.models._generated.enums.FvRouteScp import FvRouteScp
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        q = Query(fvSubnet, MagicMock()).where(scope={FvRouteScp.PRIVATE, FvRouteScp.PUBLIC})
        _, params = q.build()
        assert params["query-target-filter"] == 'eq(fvSubnet.scope,"public,private")'

    def test_between_coerces_non_string_bounds(self) -> None:
        from niwaki.query import between

        assert self._filter(pri=between(1, 5)) == 'bw(fvBD.pri,"1","5")'


# ── Limit interaction with aggregates, and the __bool__ guard ─────────────────


class TestLimitAndBool:
    def test_count_honors_limit(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "10", "imdata": []})
        assert Query(fvBD, session)[:3].count() == 3

    def test_count_below_limit_returns_total(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "2", "imdata": []})
        assert Query(fvBD, session)[:10].count() == 2

    def test_count_zero_limit_skips_request(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "5", "imdata": []})
        assert Query(fvBD, session)[:0].count() == 0
        session._request_checked.assert_not_called()

    def test_exists_false_when_limit_zero(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _make_session(count_response={"totalCount": "5", "imdata": []})
        assert Query(fvBD, session)[:0].exists() is False
        session._request_checked.assert_not_called()

    def test_bool_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(TypeError, match="no boolean value"):
            bool(Query(fvBD, MagicMock()))

    def test_slice_stops_before_requesting_next_page(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant

        pages_consumed: list[int] = []

        def gen(*_args: object, **_kwargs: object) -> Any:
            for i in range(5):
                pages_consumed.append(i)
                yield [_fvTenant_item(f"t{i}")]

        session = MagicMock()
        session._iter_pages = gen
        got = list(Query(fvTenant, session)[:1])
        assert len(got) == 1
        assert pages_consumed == [0]  # the second page is never requested


# ── subscribe(): rejection matrix (fail loud, zero I/O) ────────────────────────


def _subscribing_session() -> MagicMock:
    """A mock session whose subscribe() returns a minimal, valid RawSubscription-shaped stub."""
    session = MagicMock()
    session.subscribe.return_value.initial = []
    return session


class TestSubscribeRejections:
    def test_order_by_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        with pytest.raises(ValueError, match="order_by"):
            Query(fvBD, session).order_by("name").subscribe()
        session.subscribe.assert_not_called()

    def test_slice_limit_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        with pytest.raises(ValueError, match="slice limit"):
            Query(fvBD, session)[:5].subscribe()
        session.subscribe.assert_not_called()

    def test_unlimited_slice_is_not_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        Query(fvBD, session)[:].subscribe()
        session.subscribe.assert_called_once()

    def test_also_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        session = _subscribing_session()
        with pytest.raises(ValueError, match="also"):
            Query(fvBD, session).under("uni/tn-prod").also(fvSubnet).subscribe()
        session.subscribe.assert_not_called()

    @pytest.mark.parametrize(
        "accumulate",
        [
            lambda q: q.with_faults(),
            lambda q: q.with_health(),
            lambda q: q.with_stats(),
            lambda q: q.with_relations(),
            lambda q: q.only_faulted(),
            lambda q: q.subtree_full(),
        ],
    )
    def test_subtree_enrichment_shortcuts_are_rejected(self, accumulate: Any) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        with pytest.raises(ValueError, match="subtree enrichment"):
            accumulate(Query(fvBD, session)).subscribe()
        session.subscribe.assert_not_called()

    def test_include_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        session = _subscribing_session()
        with pytest.raises(ValueError, match="subtree enrichment"):
            Query(fvBD, session).include(fvSubnet).subscribe()
        session.subscribe.assert_not_called()

    def test_include_subtree_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query._base import SubtreeInclude

        session = _subscribing_session()
        with pytest.raises(ValueError, match="subtree enrichment"):
            Query(fvBD, session).include_subtree(SubtreeInclude.FAULTS).subscribe()
        session.subscribe.assert_not_called()

    def test_subtree_where_is_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        session = _subscribing_session()
        with pytest.raises(ValueError, match="subtree enrichment"):
            Query(fvBD, session).include(fvSubnet).subtree_where(ip="10.0.0.1/24").subscribe()
        session.subscribe.assert_not_called()

    def test_naming_only_is_allowed(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        Query(fvBD, session).naming_only().subscribe()
        session.subscribe.assert_called_once()

    def test_config_only_is_allowed(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        Query(fvBD, session).config_only().subscribe()
        session.subscribe.assert_called_once()

    def test_where_and_under_are_allowed(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        Query(fvBD, session).under("uni/tn-prod").where(name="web").subscribe()
        session.subscribe.assert_called_once()
        path, params = session.subscribe.call_args[0][:2]
        assert path == "/api/mo/uni/tn-prod.json"
        assert params["query-target-filter"] == 'eq(fvBD.name,"web")'


class TestSubscribeStatsGuard:
    def test_stats_class_raises_before_any_io(self) -> None:
        from niwaki import exceptions

        session = _subscribing_session()
        with pytest.raises(exceptions.StatsClassNotSubscribableError):
            Query("acllogFlowCounter15min", session).subscribe()
        session.subscribe.assert_not_called()

    def test_configurable_class_does_not_raise(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = _subscribing_session()
        Query(fvBD, session).subscribe()
        session.subscribe.assert_called_once()

    def test_unknown_class_fails_open(self) -> None:
        session = _subscribing_session()
        Query("totallyUnknownClassXYZ", session).subscribe()
        session.subscribe.assert_called_once()
