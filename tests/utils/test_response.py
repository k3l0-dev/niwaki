"""Tests for niwaki.utils.response.

Covers: parse_imdata — nominal, error entries skipped, deserialisation failures.
"""

from __future__ import annotations

import pytest

from niwaki.exceptions import DeserializationError
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models.base import ManagedObject
from niwaki.utils.response import parse_imdata

# ── Fixtures ──────────────────────────────────────────────────────────────────

_BD_ITEM = {"fvBD": {"attributes": {"name": "web"}}}
_TNT_ITEM = {"fvTenant": {"attributes": {"name": "prod"}}}
_ERR_ITEM = {"error": {"attributes": {"code": "400", "text": "Bad request"}}}


# ── parse_imdata ──────────────────────────────────────────────────────────────


class TestParseImdata:
    def test_nominal_single_object(self) -> None:
        raw = {"totalCount": "1", "imdata": [_BD_ITEM]}
        result = parse_imdata(raw)
        assert len(result) == 1
        assert isinstance(result[0], fvBD)
        assert result[0].name == "web"

    def test_nominal_multiple_objects(self) -> None:
        raw = {"imdata": [_BD_ITEM, _TNT_ITEM]}
        result = parse_imdata(raw)
        assert len(result) == 2
        assert isinstance(result[0], fvBD)
        assert isinstance(result[1], fvTenant)

    def test_empty_imdata_returns_empty_list(self) -> None:
        assert parse_imdata({"imdata": []}) == []

    def test_missing_imdata_returns_empty_list(self) -> None:
        assert parse_imdata({}) == []

    def test_error_items_are_skipped(self) -> None:
        raw = {"imdata": [_ERR_ITEM, _BD_ITEM]}
        result = parse_imdata(raw)
        assert len(result) == 1
        assert isinstance(result[0], fvBD)

    def test_unknown_class_falls_back_to_managed_object(self) -> None:
        raw = {"imdata": [{"unknownClass": {"attributes": {"name": "x"}}}]}
        result = parse_imdata(raw)
        assert len(result) == 1
        assert type(result[0]) is ManagedObject

    def test_malformed_item_raises_deserialization_error(self) -> None:
        # An empty dict has no class key — from_apic raises StopIteration
        # which parse_imdata wraps into DeserializationError.
        with pytest.raises(DeserializationError):
            parse_imdata({"imdata": [{}]})
