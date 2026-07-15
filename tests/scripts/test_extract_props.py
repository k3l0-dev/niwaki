"""The property extractor — where a schema becomes a Python type.

Three defects lived here, and all three were invisible:

1. an unclassified ``baseType`` silently became ``str`` (half the SDK's numbers
   were text);
2. only ``validators[0]`` was read, so a property with two ranges got the first
   one — ``bgpCtxPol.holdIntvl`` declares ``[{0,0}, {3,3600}]`` and a model built
   from the first validator alone **rejects its own default of 180**;
3. the numeric weight of an enum member was thrown away by an alphabetical
   sort, and it is exactly what a bitmask needs: the APIC serialises flags in
   ascending bit weight.

The schema fragments below are copied from the real APIC 6.0 schemas — never
invented.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType
from typing import Any, ClassVar

import pytest

from niwaki._codegen.basetypes import UnknownBaseTypeError


def _load_extractor() -> ModuleType:
    """Import the numbered extraction script (its name is not an identifier)."""
    path = pathlib.Path(__file__).parents[2] / "data" / "scripts" / "02_extract_props.py"
    spec = importlib.util.spec_from_file_location("extract_props", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_props"] = module
    spec.loader.exec_module(module)
    return module


extract = _load_extractor()


class TestFailLoud:
    """An unknown family raises instead of degrading."""

    def test_an_unclassified_base_type_raises(self) -> None:
        with pytest.raises(UnknownBaseTypeError):
            extract.normalize_prop({"baseType": "scalar:Nonesuch"})

    def test_no_base_type_at_all_maps_to_nothing(self) -> None:
        """Not an error — the schema simply describes no value."""
        assert extract.normalize_prop({}) is None


class TestNumericRange:
    """The bounds a number accepts — across *every* validator, not the first."""

    def test_two_ranges_are_unioned(self) -> None:
        """``bgpCtxPol.holdIntvl``: the value 0, or the range [3, 3600] — default 180.

        Reading ``validators[0]`` alone yields ``le=0`` — a model that rejects
        its own default.
        """
        ge, le = extract._numeric_range([{"min": 0, "max": 0}, {"min": 3, "max": 3600}])
        assert (ge, le) == (0, 3600)

    def test_a_zero_maximum_means_unbounded(self) -> None:
        """``bgpAsP.asn`` declares ``{min: 1, max: 0}``.

        Read literally that is an empty range, and a model built from it would
        reject every AS number ever written.  Zero is Cisco's sentinel for "no
        ceiling", not a ceiling.
        """
        ge, le = extract._numeric_range([{"min": 1, "max": 0}])
        assert (ge, le) == (1, None)

    def test_no_validators_means_no_bounds(self) -> None:
        assert extract._numeric_range([]) == (None, None)

    def test_a_single_ordinary_range(self) -> None:
        assert extract._numeric_range([{"min": 1, "max": 65535}]) == (1, 65535)


class TestStringConstraints:
    """A pattern does not always live in the first validator."""

    def test_the_regex_is_found_wherever_it_sits(self) -> None:
        """``vmmInjectedSvcEp.name`` keeps its regex in ``validators[1]`` — and a
        pattern silently dropped is a validation the SDK claims and never does."""
        _, _, pattern = extract._string_constraints(
            [{"min": 0, "max": 512}, {"regexs": [{"regex": "^[a-z]+$"}]}]
        )
        assert pattern == "^[a-z]+$"

    def test_lengths_span_every_validator(self) -> None:
        min_length, max_length, _ = extract._string_constraints(
            [{"min": 1, "max": 16}, {"min": 0, "max": 64}]
        )
        assert (min_length, max_length) == (0, 64)

    def test_no_validators(self) -> None:
        assert extract._string_constraints([]) == (0, None, None)


class TestEnumValues:
    """The numeric weight of a member is carried through — a bitmask needs it."""

    #: ``fvSubnet.scope`` — the real schema fragment.
    SCOPE: ClassVar[list[dict[str, Any]]] = [
        {"value": "private", "localName": "defaultValue"},
        {"value": "2", "localName": "private"},
        {"value": "1", "localName": "public"},
        {"value": "4", "localName": "shared"},
    ]

    def test_members_and_aliases(self) -> None:
        values, aliases, _, _ = extract._extract_enum_values(self.SCOPE)
        assert values == ["private", "public", "shared"]
        assert aliases == {"2": "private", "1": "public", "4": "shared"}

    def test_the_bit_weights_survive(self) -> None:
        """Sorting alphabetically threw these away, and nothing downstream could
        reconstruct them — yet they are the order the APIC serialises in."""
        _, _, _, weights = extract._extract_enum_values(self.SCOPE)
        assert weights == {"private": 2, "public": 1, "shared": 4}

    def test_the_apic_order_is_recoverable_from_the_weights(self) -> None:
        """A scope of {shared, public} is stored ``"public,shared"`` — 1 then 4."""
        _, _, _, weights = extract._extract_enum_values(self.SCOPE)
        by_weight = sorted({"shared", "public"}, key=lambda member: weights[member])
        assert ",".join(by_weight) == "public,shared"

    def test_hex_weights(self) -> None:
        """Ether types are hex: ``{'value': '0x806', 'localName': 'arp'}``."""
        _, aliases, _, weights = extract._extract_enum_values(
            [{"value": "0x806", "localName": "arp"}]
        )
        assert aliases == {"0x806": "arp"}
        assert weights == {"arp": 2054}

    def test_a_comment_reaches_the_member(self) -> None:
        _, _, comments, _ = extract._extract_enum_values(
            [{"value": "1", "localName": "public", "comment": ["Advertised externally."]}]
        )
        assert comments == {"public": "Advertised externally."}

    def test_an_enum_with_no_values(self) -> None:
        assert extract._extract_enum_values([]) == ([], {}, {}, {})


class TestNormalizeProp:
    """Each family, end to end."""

    def test_bool(self) -> None:
        prop = extract.normalize_prop({"baseType": "scalar:Bool", "default": "yes"})
        assert prop is not None
        assert prop["python_type"] == "bool"
        assert prop["default"] is True

    def test_int_carries_the_union_of_its_ranges(self) -> None:
        prop = extract.normalize_prop(
            {
                "baseType": "scalar:Uint32",
                "default": "180",
                "validators": [{"min": 0, "max": 0}, {"min": 3, "max": 3600}],
            }
        )
        assert prop is not None
        assert (prop["ge"], prop["le"], prop["default"]) == (0, 3600, 180)

    def test_a_string_keeps_its_length_and_pattern(self) -> None:
        prop = extract.normalize_prop(
            {
                "baseType": "string:Basic",
                "validators": [{"min": 1, "max": 64, "regexs": [{"regex": "^[a-z]+$"}]}],
            }
        )
        assert prop is not None
        assert (prop["min_length"], prop["max_length"], prop["pattern"]) == (1, 64, "^[a-z]+$")

    def test_a_naming_string_has_no_default(self) -> None:
        """The caller must name the object; there is nothing to fall back to."""
        prop = extract.normalize_prop({"baseType": "string:Basic", "isNaming": True})
        assert prop is not None
        assert prop["default"] is None

    def test_an_address_is_validated(self) -> None:
        prop = extract.normalize_prop({"baseType": "address:Ip"})
        assert prop is not None
        assert prop["validate_as"] == "ip"

    def test_a_deliberate_string_carries_no_constraint(self) -> None:
        """A DN is opaque: no length, no pattern, nothing to check."""
        prop = extract.normalize_prop({"baseType": "reference:BinRef"})
        assert prop is not None
        assert prop["python_type"] == "str"
        assert "pattern" not in prop and "min_length" not in prop

    def test_the_schema_default_is_kept_verbatim(self) -> None:
        """A flags default is a comma-joined set — no single-value rule could
        ever reconstruct it, so the raw string travels with the property."""
        prop = extract.normalize_prop(
            {"baseType": "scalar:Bitmask32", "default": "susp-individual,graceful-conv"}
        )
        assert prop is not None
        assert prop["schema_default"] == "susp-individual,graceful-conv"
