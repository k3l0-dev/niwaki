"""The ACI type map — and the guard that keeps it exhaustive.

The bug this map exists to end was not a wrong mapping; it was a **missing**
one.  The extractor knew six families and silently made everything else a
string, which is how half the SDK's numbers became text and how bitmasks became
unusable.  A silent fallback does not fail — it degrades, and a degradation
nobody sees is a bug that ships.

So the contract asserted here is:

* every ``baseType`` the schemas actually use is classified — a firmware that
  invents one must break **this** build, not a user's code;
* an unclassified family raises, loudly;
* a family still being migrated is *declared* as such, never merely forgotten.

The corpus arm needs the raw schemas (1.7 GB, not in the repository), so it
skips where they are absent — exactly like the other schema-derived guards.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from niwaki._codegen.basetypes import (
    BASETYPE_MAP,
    FieldKind,
    UnknownBaseTypeError,
    kind_for,
)

_SCHEMAS_DIR = pathlib.Path(__file__).parents[2] / "data" / "schemas" / "mo-apic-v6.0_9c"


class TestKindFor:
    """Nominal: the families the SDK actually meets."""

    @pytest.mark.parametrize(
        ("base_type", "kind"),
        [
            ("scalar:Bool", FieldKind.BOOL),
            ("scalar:Enum8", FieldKind.ENUM),
            ("scalar:Uint32", FieldKind.INT),
            ("address:Ip", FieldKind.IP),
            ("address:MAC", FieldKind.MAC),
            ("string:Basic", FieldKind.STR),
            ("reference:BinRef", FieldKind.STR),
        ],
    )
    def test_settled_families(self, base_type: str, kind: FieldKind) -> None:
        assert kind_for(base_type) is kind

    def test_an_unclassified_family_raises(self) -> None:
        """The whole point: guessing is what produced the typing we are fixing."""
        with pytest.raises(UnknownBaseTypeError, match="unclassified schema baseType"):
            kind_for("scalar:Nonesuch")

    def test_the_error_says_what_to_do(self) -> None:
        with pytest.raises(UnknownBaseTypeError, match="never let it fall back to str"):
            kind_for("quantum:Flux")


class TestEveryFamilyIsSettled:
    """The migration is complete: every family renders its true kind, no shadow."""

    def test_every_address_is_validated_now(self) -> None:
        """The last phase.  IPv4 and IPv6 join the generic IP regex (it accepts
        both); MACPrefix does *not* become a MAC — its five props are Fibre
        Channel identifiers (fcId, fcMap), and the 6-octet MAC regex would reject
        every legitimate value, so it is a validated-by-the-APIC string on
        purpose."""
        assert kind_for("address:IPv4") is FieldKind.IP
        assert kind_for("address:IPv6") is FieldKind.IP
        assert kind_for("address:MACPrefix") is FieldKind.STR

    def test_every_number_is_a_number_now(self) -> None:
        """``bgpCtxPol.hold_interval`` (a Uint16) was text while
        ``lacpLagPol.max_links`` (a Uint32) was an int — same semantics, different
        type, decided by which branch the extractor happened to have."""
        for family in ("scalar:Uint16", "scalar:UByte", "scalar:Uint64", "scalar:Seconds"):
            assert kind_for(family) is FieldKind.INT
        for family in ("scalar:Float", "scalar:Double"):
            assert kind_for(family) is FieldKind.FLOAT

    def test_every_bitmask_is_a_set_now(self) -> None:
        """The family the whole overhaul started from.

        ``Bitmask32`` used to be rendered as a *single-choice* enum, which is why
        ``vzEntry(tcp_rules="syn,ack")`` — an everyday ACI filter — was rejected
        by the SDK's own model.  The other three bitmask widths were bare strings:
        no validation, no allowed values in the documentation.  All four are sets.
        """
        for width in (
            "scalar:Bitmask8",
            "scalar:Bitmask16",
            "scalar:Bitmask32",
            "scalar:Bitmask64",
        ):
            assert kind_for(width) is FieldKind.FLAGS


class TestEveryStringIsADecision:
    """``str`` is still right for a DN or a password — but it must be *chosen*."""

    @pytest.mark.parametrize(
        "base_type",
        ["reference:BinRef", "base:Encap", "string:Password", "mo:MoClassId", "scalar:Date"],
    )
    def test_the_deliberate_strings_are_in_the_map(self, base_type: str) -> None:
        assert BASETYPE_MAP[base_type] is FieldKind.STR

    def test_identifier_families_never_become_enums(self) -> None:
        """``mo:MoClassId`` carries 17,654 validValues — it is an identifier, not
        a vocabulary, and an enum of that size would be a monument to a
        misreading of the schema."""
        for base_type in ("mo:MoClassId", "mo:StatsPropId", "mo:StatsClassId"):
            assert BASETYPE_MAP[base_type] is FieldKind.STR


@pytest.mark.skipif(
    not _SCHEMAS_DIR.exists(),
    reason="raw APIC schemas not present (1.7 GB, not in the repository)",
)
class TestTheCorpusIsFullyClassified:
    """The guard that makes the next firmware release break *our* build."""

    @staticmethod
    def _corpus_base_types() -> set[str]:
        found: set[str] = set()
        for schema_file in _SCHEMAS_DIR.glob("*.json"):
            try:
                data = json.loads(schema_file.read_text())
            except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt file
                continue

            def walk(node: object) -> None:
                if isinstance(node, dict):
                    props = node.get("properties")
                    if isinstance(props, dict):
                        for prop in props.values():
                            if (
                                isinstance(prop, dict)
                                and prop.get("isConfigurable")
                                and not prop.get("isDeprecated")
                                and (base_type := prop.get("baseType"))
                            ):
                                found.add(str(base_type))
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(data)
        return found

    def test_no_family_escapes_the_map(self) -> None:
        unclassified = sorted(self._corpus_base_types() - set(BASETYPE_MAP))
        assert not unclassified, (
            f"unclassified schema baseType(s): {unclassified}. Classify them in "
            "niwaki._codegen.basetypes.BASETYPE_MAP, with the reason — the old "
            "behaviour was to guess str, and that guess is the bug."
        )

    def test_the_map_carries_no_dead_rows(self) -> None:
        """A family the corpus never uses is a decision about nothing."""
        corpus = self._corpus_base_types()
        # address:Ip4/Ip6 are historical spellings kept as a safety net.
        dead = sorted(set(BASETYPE_MAP) - corpus - {"address:Ip4", "address:Ip6"})
        assert not dead, f"BASETYPE_MAP classifies families the corpus never uses: {dead}"
