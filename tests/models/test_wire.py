"""The wire boundary — Python values ⇄ APIC attribute strings.

Everything the SDK writes goes through ``to_wire`` (``to_apic`` and, crucially,
RN computation), everything it reads comes back through ``from_wire``
(``from_apic`` builds with ``model_construct``, so no validator ever runs).

A kind the boundary can write but not read drifts against its own declaration
for ever — in ``push(mode="plan")`` and in the live verifier alike.  Hence the
round-trip tests below: they are the contract, not a formality.

The expectations here are not invented: they were measured against a 6.0(9c)
APIC — a port declared as 80 reads back as ``"http"``, a subnet scope declared
``"shared,public"`` reads back ``"public,shared"`` (ascending bit weight), an
unset flags field reads back as ``""``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import Field

from niwaki.models._wire import from_wire, to_filter, to_wire


class Scope(StrEnum):
    """A flags enum, declared in ascending bit weight — as the generator emits."""

    PUBLIC = "public"  # weight 1
    PRIVATE = "private"  # weight 2
    SHARED = "shared"  # weight 4


class Mode(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class TestToWire:
    """Python → the exact string the APIC stores."""

    @pytest.mark.parametrize(
        ("value", "wire"),
        [
            (True, "true"),
            (False, "false"),
            (180, "180"),
            (1.5, "1.5"),
            ("web", "web"),
            (Mode.ENABLED, "enabled"),
        ],
    )
    def test_scalars(self, value: object, wire: str) -> None:
        assert to_wire(value) == wire

    def test_flags_join_in_bit_order_not_alphabetical(self) -> None:
        """The APIC stores ``"public,shared"`` — weights 1 and 4, not a-z."""
        assert to_wire(frozenset({Scope.SHARED, Scope.PUBLIC})) == "public,shared"
        assert to_wire(frozenset({Scope.SHARED, Scope.PRIVATE, Scope.PUBLIC})) == (
            "public,private,shared"
        )

    def test_flags_order_does_not_depend_on_insertion(self) -> None:
        one = to_wire(frozenset([Scope.SHARED, Scope.PUBLIC]))
        other = to_wire(frozenset([Scope.PUBLIC, Scope.SHARED]))
        assert one == other == "public,shared"

    def test_empty_flag_set(self) -> None:
        """An APIC bitmask with nothing set reads back as an empty string."""
        assert to_wire(frozenset()) == ""

    def test_a_single_flag(self) -> None:
        assert to_wire(frozenset({Scope.PRIVATE})) == "private"

    def test_plain_strings_in_a_set_fall_back_to_sorted(self) -> None:
        """No enum to order by — deterministic beats arbitrary."""
        assert to_wire(frozenset({"b", "a"})) == "a,b"


class TestToFilter:
    """A filter must render a value the way the APIC stores it, or never match."""

    def test_bools_use_the_filter_grammar(self) -> None:
        assert to_filter(True) == "yes"
        assert to_filter(False) == "no"

    def test_everything_else_matches_the_wire(self) -> None:
        assert to_filter(80) == "80"
        assert to_filter(Mode.DISABLED) == "disabled"
        assert to_filter(frozenset({Scope.SHARED, Scope.PUBLIC})) == "public,shared"


class TestFromWire:
    """APIC string → the declared Python type."""

    @pytest.mark.parametrize(("raw", "expected"), [("yes", True), ("true", True), ("no", False)])
    def test_bool(self, raw: str, expected: bool) -> None:
        assert from_wire(bool, raw) is expected

    def test_int(self) -> None:
        assert from_wire(int, "180") == 180
        assert from_wire(Annotated[int, Field(ge=0)], "180") == 180

    def test_float(self) -> None:
        assert from_wire(float, "1.5") == 1.5

    def test_enum_member_not_a_bare_string(self) -> None:
        """``from_apic`` skips validators — without this, an enum field holds a str."""
        value = from_wire(Mode, "enabled")
        assert value is Mode.ENABLED
        assert isinstance(value, Mode)

    def test_flags(self) -> None:
        assert from_wire(frozenset[Scope], "public,shared") == frozenset(
            {Scope.PUBLIC, Scope.SHARED}
        )

    def test_flags_ignore_the_order_the_apic_used(self) -> None:
        """The whole point: the APIC reorders, and a set does not care."""
        assert from_wire(frozenset[Scope], "shared,public") == from_wire(
            frozenset[Scope], "public,shared"
        )

    def test_empty_flags(self) -> None:
        assert from_wire(frozenset[Scope], "") == frozenset()

    def test_int_or_keyword(self) -> None:
        """The APIC canonicalises: port 80 is stored as ``"http"``."""
        annotation = int | Literal["http", "unspecified"]
        assert from_wire(annotation, "8080") == 8080
        assert from_wire(annotation, "http") == "http"
        assert from_wire(annotation, "unspecified") == "unspecified"

    def test_hex_sentinels_survive(self) -> None:
        """Some numeric props hold ``0xffffffffffffffff`` — a number, in hex."""
        assert from_wire(int, "0xff") == 255

    def test_a_string_stays_a_string(self) -> None:
        assert from_wire(str, "uni/tn-prod") == "uni/tn-prod"

    def test_none_is_left_alone(self) -> None:
        assert from_wire(int, None) is None


class TestRobustness:
    """The APIC is trusted: a surprising read is never an exception."""

    def test_an_unparseable_int_is_handed_back(self) -> None:
        assert from_wire(int, "not-a-number") == "not-a-number"

    def test_an_unknown_enum_value_is_handed_back(self) -> None:
        """A firmware newer than our schemas may return a value we do not know."""
        assert from_wire(Mode, "quantum") == "quantum"

    def test_an_unknown_flag_member_hands_the_raw_value_back(self) -> None:
        assert from_wire(frozenset[Scope], "public,teleport") == "public,teleport"

    def test_an_unparseable_bool_is_handed_back(self) -> None:
        assert from_wire(bool, "maybe") == "maybe"


class TestRoundTrip:
    """``to_wire ∘ from_wire`` is the identity on every kind the SDK declares."""

    @pytest.mark.parametrize(
        ("annotation", "wire"),
        [
            (bool, "true"),
            (bool, "false"),
            (int, "180"),
            (int, "0"),
            (float, "1.5"),
            (Mode, "enabled"),
            (frozenset[Scope], "public,shared"),
            (frozenset[Scope], "private"),
            (frozenset[Scope], ""),
            (int | Literal["http"], "8080"),
            (int | Literal["http"], "http"),
            (str, "uni/tn-prod"),
        ],
    )
    def test_wire_survives_a_round_trip(self, annotation: object, wire: str) -> None:
        assert to_wire(from_wire(annotation, wire)) == wire

    def test_a_reordered_flag_string_normalises(self) -> None:
        """The one case that is deliberately NOT the identity — and must not be."""
        assert to_wire(from_wire(frozenset[Scope], "shared,public")) == "public,shared"
