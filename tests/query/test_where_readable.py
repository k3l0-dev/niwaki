"""Readable (Python) property names in ``where()`` and ``subtree_where()``.

The keyword shorthand accepts, besides the wire name of a property
(``arpFlood=True``), the readable field name the models expose
(``arp_flooding=True``).  The proof is offline and byte-level: the query a
readable keyword renders (``build()``) is **byte-equal** to the one the same
Python value renders under the wire keyword.

Key resolution, per queried class (or per *included* class for
``subtree_where``):

1. the key is the wire name of a property → used as-is (today's behaviour);
2. otherwise the key is the readable name of a property → its wire name;
3. a key that is *both* the wire name of one property and the readable name of
   **another** is ambiguous → ``ValueError`` naming both readings and pointing
   to a qualified ``eq()``;
4. an unknown key keeps today's behaviour: it is qualified and passed through
   verbatim (no error).

Collisions are real in the shipped models (four, found by scanning every
generated class): ``version`` on ``firmwareFwP`` / ``firmwareCatFwP`` /
``firmwareCtrlrFwP`` (wire ``version`` whose readable name is
``target_firmware_version``, and readable ``version`` of wire ``srVersion``),
and ``description`` on ``mdpProviderP`` (wire ``description`` read as
``multi_domain_profile_description``, and readable ``description`` of wire
``descr``).  The shipped catalogue has **no** collision among the classes it
serves without a model, so the catalogue side of the rule is pinned with a
single synthetic :class:`ClassMeta` planted in the catalogue's memo cache.

Every test runs against both builders — :class:`AsyncQuery` is the source the
sync :class:`Query` is generated from, and both must render identically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import MagicMock

import pytest

from niwaki.models._generated.enums.L2UnkMacUcastAct import L2UnkMacUcastAct
from niwaki.models._generated.enums.L4TcpFlags import L4TcpFlags
from niwaki.models.aaa.aaaBlockLoginProfile import aaaBlockLoginProfile
from niwaki.models.base import ManagedObject
from niwaki.models.firmware.firmwareCatFwP import firmwareCatFwP
from niwaki.models.firmware.firmwareCtrlrFwP import firmwareCtrlrFwP
from niwaki.models.firmware.firmwareFwP import firmwareFwP
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.fv.fvSubnet import fvSubnet
from niwaki.models.mdp.mdpProviderP import mdpProviderP
from niwaki.models.vz.vzEntry import vzEntry
from niwaki.query import (
    AsyncQuery,
    FilterValue,
    Query,
    any_of,
    between,
    eq,
    like,
    wcard,
)
from niwaki.query._catalog import ClassMeta, catalog

# ── Builders under test ───────────────────────────────────────────────────────

type _AnyQuery = Query[ManagedObject] | AsyncQuery[ManagedObject]
type _Factory = Callable[[type[ManagedObject] | str], _AnyQuery]

_FILTER = "query-target-filter"
_SUBTREE_FILTER = "rsp-subtree-filter"


def _sync(cls: type[ManagedObject] | str) -> _AnyQuery:
    return Query(cls, MagicMock())


def _async(cls: type[ManagedObject] | str) -> _AnyQuery:
    return AsyncQuery(cls, MagicMock())


@pytest.fixture(params=[_sync, _async], ids=["sync", "async"])
def make(request: pytest.FixtureRequest) -> _Factory:
    """Build a query of either flavour — every test runs against both."""
    factory: _Factory = request.param
    return factory


def _where(
    make: _Factory, cls: type[ManagedObject] | str, kwargs: dict[str, FilterValue]
) -> dict[str, str]:
    """The ``build()`` params of ``make(cls).where(**kwargs)``."""
    return make(cls).where(**kwargs).build()[1]


def _filter(make: _Factory, cls: type[ManagedObject] | str, **kwargs: FilterValue) -> str:
    """The rendered ``query-target-filter`` of ``make(cls).where(**kwargs)``."""
    return _where(make, cls, kwargs)[_FILTER]


def _readable_bool_fields(cls: type[ManagedObject]) -> Iterator[tuple[str, str]]:
    """``(readable, wire)`` for every renamed ``bool`` field of a generated model."""
    for readable, field in cls.model_fields.items():
        alias = field.validation_alias
        if field.annotation is bool and isinstance(alias, str) and alias != readable:
            yield readable, alias


# ── Generated class: readable name ≡ wire name, byte for byte ─────────────────


class TestReadableNameGeneratedClass:
    @pytest.mark.parametrize("value", [True, False])
    def test_bool_readable_is_byte_equal_to_wire(self, make: _Factory, value: bool) -> None:
        readable = _where(make, fvBD, {"arp_flooding": value})
        wire = _where(make, fvBD, {"arpFlood": value})
        assert readable == wire

    def test_bool_renders_the_filter_spelling(self, make: _Factory) -> None:
        # The filter grammar's booleans (yes/no) — the spelling where(arpFlood=True)
        # renders today and that was validated against a live fabric.
        assert _filter(make, fvBD, arp_flooding=True) == 'eq(fvBD.arpFlood,"yes")'
        assert _filter(make, fvBD, arp_flooding=False) == 'eq(fvBD.arpFlood,"no")'

    def test_bool_given_as_wire_string(self, make: _Factory) -> None:
        assert _filter(make, fvBD, arp_flooding="yes") == _filter(make, fvBD, arpFlood="yes")
        assert _filter(make, fvBD, arp_flooding="yes") == 'eq(fvBD.arpFlood,"yes")'

    @pytest.mark.parametrize(
        ("readable", "wire"),
        list(_readable_bool_fields(fvBD)),
        ids=lambda name: str(name),
    )
    def test_every_renamed_bool_field_of_fvBD(
        self, make: _Factory, readable: str, wire: str
    ) -> None:
        assert _filter(make, fvBD, **{readable: True}) == f'eq(fvBD.{wire},"yes")'

    def test_int_field(self, make: _Factory) -> None:
        got = _filter(make, aaaBlockLoginProfile, block_duration=60)
        assert got == 'eq(aaaBlockLoginProfile.blockDuration,"60")'
        assert got == _filter(make, aaaBlockLoginProfile, blockDuration=60)

    def test_named_number_given_by_name(self, make: _Factory) -> None:
        got = _filter(make, vzEntry, destination_from_port="https")
        assert got == 'eq(vzEntry.dFromPort,"https")'
        assert got == _filter(make, vzEntry, dFromPort="https")

    def test_named_number_given_as_number_matches_wire_form(self, make: _Factory) -> None:
        # Byte-equality with the wire keyword carrying the same Python value.
        # The APIC compares named numbers semantically (measured live: a filter
        # on 80 matches an entry stored as "http"), so the number is not renamed.
        got = _filter(make, vzEntry, destination_from_port=80)
        assert got == _filter(make, vzEntry, dFromPort=80)
        assert got == 'eq(vzEntry.dFromPort,"80")'

    def test_number_without_a_name(self, make: _Factory) -> None:
        got = _filter(make, vzEntry, destination_from_port=8080)
        assert got == 'eq(vzEntry.dFromPort,"8080")'

    def test_bitmask_from_a_set_of_members(self, make: _Factory) -> None:
        flags: FilterValue = {L4TcpFlags.SYN, L4TcpFlags.ACK}
        got = _filter(make, vzEntry, tcp_rules=flags)
        assert got == 'eq(vzEntry.tcpRules,"syn,ack")'
        assert got == _filter(make, vzEntry, tcpRules=flags)

    def test_bitmask_from_a_set_of_names_matches_wire_form(self, make: _Factory) -> None:
        # A set stays a bitmask equality (never a membership OR).
        got = _filter(make, vzEntry, tcp_rules={"syn", "ack"})
        assert got == _filter(make, vzEntry, tcpRules={"syn", "ack"})
        assert got.startswith("eq(vzEntry.tcpRules,")

    def test_bitmask_from_a_frozenset(self, make: _Factory) -> None:
        flags = frozenset({L4TcpFlags.EST})
        assert _filter(make, vzEntry, tcp_rules=flags) == 'eq(vzEntry.tcpRules,"est")'

    def test_enum_member(self, make: _Factory) -> None:
        got = _filter(make, fvBD, unknown_mac_unicast_action=L2UnkMacUcastAct.PROXY)
        assert got == 'eq(fvBD.unkMacUcastAct,"proxy")'
        assert got == _filter(make, fvBD, unkMacUcastAct=L2UnkMacUcastAct.PROXY)

    def test_enum_given_as_string(self, make: _Factory) -> None:
        got = _filter(make, fvBD, unknown_mac_unicast_action="flood")
        assert got == 'eq(fvBD.unkMacUcastAct,"flood")'

    def test_string_field(self, make: _Factory) -> None:
        got = _filter(make, fvBD, description="web tier")
        assert got == 'eq(fvBD.descr,"web tier")'
        assert got == _filter(make, fvBD, descr="web tier")

    def test_readable_name_that_is_also_its_own_wire_name(self, make: _Factory) -> None:
        # ``name`` is both the wire and the readable name of the SAME property.
        assert _filter(make, fvBD, name="web") == 'eq(fvBD.name,"web")'

    def test_renamed_naming_property(self, make: _Factory) -> None:
        # mdpProviderP reads its wire ``name`` as ``domain_name``.
        assert _filter(make, mdpProviderP, domain_name="d1") == 'eq(mdpProviderP.name,"d1")'
        assert _filter(make, mdpProviderP, name="d1") == 'eq(mdpProviderP.name,"d1")'

    def test_readable_and_wire_mixed_in_one_call_keep_order(self, make: _Factory) -> None:
        got = _filter(make, fvBD, arp_flooding=True, unicastRoute=False)
        assert got == 'and(eq(fvBD.arpFlood,"yes"),eq(fvBD.unicastRoute,"no"))'
        assert got == _filter(make, fvBD, arpFlood=True, unicast_routing=False)

    def test_chained_where_calls(self, make: _Factory) -> None:
        q = make(fvBD).where(arp_flooding=True).where(unicast_routing=True)
        assert q.build()[1][_FILTER] == ('and(eq(fvBD.arpFlood,"yes"),eq(fvBD.unicastRoute,"yes"))')

    def test_where_is_still_immutable(self, make: _Factory) -> None:
        base = make(fvBD)
        base.where(arp_flooding=True)
        assert _FILTER not in base.build()[1]

    def test_readable_names_through_the_string_class_name_of_a_model(self, make: _Factory) -> None:
        # ``aci.query("fvBD")`` addresses a generated class by name: same rule.
        assert _filter(make, "fvBD", arp_flooding=True) == 'eq(fvBD.arpFlood,"yes")'


# ── Smart kwargs compose with the translation ─────────────────────────────────


class TestSmartKwargsWithReadableNames:
    def test_list_is_a_membership_or(self, make: _Factory) -> None:
        got = _filter(make, fvBD, unknown_mac_unicast_action=["flood", "proxy"])
        assert got == ('or(eq(fvBD.unkMacUcastAct,"flood"),eq(fvBD.unkMacUcastAct,"proxy"))')
        assert got == _filter(make, fvBD, unkMacUcastAct=["flood", "proxy"])

    def test_tuple_is_a_membership_or(self, make: _Factory) -> None:
        got = _filter(make, vzEntry, destination_from_port=("http", "https"))
        assert got == 'or(eq(vzEntry.dFromPort,"http"),eq(vzEntry.dFromPort,"https"))'

    def test_membership_of_numbers_matches_wire_form(self, make: _Factory) -> None:
        got = _filter(make, vzEntry, destination_from_port=[80, 443])
        assert got == _filter(make, vzEntry, dFromPort=[80, 443])

    def test_single_element_list(self, make: _Factory) -> None:
        got = _filter(make, fvBD, arp_flooding=[True])
        assert got == 'eq(fvBD.arpFlood,"yes")'

    def test_any_of(self, make: _Factory) -> None:
        got = _filter(make, fvBD, unknown_mac_unicast_action=any_of("flood", "proxy"))
        assert got == ('or(eq(fvBD.unkMacUcastAct,"flood"),eq(fvBD.unkMacUcastAct,"proxy"))')
        assert got == _filter(make, fvBD, unkMacUcastAct=any_of("flood", "proxy"))

    def test_star_is_a_wildcard(self, make: _Factory) -> None:
        # The value is a pattern, not a field value: ``*`` is outside the
        # field's own pattern and must not be validated against it.
        got = _filter(make, fvBD, display_name="prod-*")
        assert got == 'wcard(fvBD.nameAlias,"prod-*")'
        assert got == _filter(make, fvBD, nameAlias="prod-*")

    def test_like(self, make: _Factory) -> None:
        got = _filter(make, fvBD, display_name=like("prod"))
        assert got == 'wcard(fvBD.nameAlias,"prod")'

    def test_between(self, make: _Factory) -> None:
        got = _filter(make, aaaBlockLoginProfile, block_duration=between(5, 60))
        assert got == 'bw(aaaBlockLoginProfile.blockDuration,"5","60")'
        assert got == _filter(make, aaaBlockLoginProfile, blockDuration=between(5, 60))

    def test_none_is_still_refused(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="None"):
            make(fvBD).where(**{"arp_flooding": None})  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]

    def test_empty_collection_is_still_refused(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="empty"):
            make(fvBD).where(unknown_mac_unicast_action=[])

    def test_filter_expression_as_keyword_value_is_still_refused(self, make: _Factory) -> None:
        with pytest.raises(TypeError, match="positionally"):
            make(fvBD).where(**{"arp_flooding": eq("fvBD.arpFlood", True)})  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ── Wire names: exactly today's behaviour (regression pins) ───────────────────


_WIRE_REGRESSIONS: list[tuple[type[ManagedObject] | str, dict[str, FilterValue], str]] = [
    (fvBD, {"arpFlood": True}, 'eq(fvBD.arpFlood,"yes")'),
    (fvBD, {"arpFlood": "yes"}, 'eq(fvBD.arpFlood,"yes")'),
    (
        fvBD,
        {"arpFlood": True, "unicastRoute": False},
        'and(eq(fvBD.arpFlood,"yes"),eq(fvBD.unicastRoute,"no"))',
    ),
    (fvBD, {"name": "web"}, 'eq(fvBD.name,"web")'),
    (fvBD, {"descr": "web tier"}, 'eq(fvBD.descr,"web tier")'),
    (fvBD, {"nameAlias": "prod-*"}, 'wcard(fvBD.nameAlias,"prod-*")'),
    (fvBD, {"unkMacUcastAct": L2UnkMacUcastAct.PROXY}, 'eq(fvBD.unkMacUcastAct,"proxy")'),
    (vzEntry, {"tcpRules": {"syn", "ack"}}, 'eq(vzEntry.tcpRules,"ack,syn")'),
    (vzEntry, {"tcpRules": {L4TcpFlags.SYN, L4TcpFlags.ACK}}, 'eq(vzEntry.tcpRules,"syn,ack")'),
    (vzEntry, {"dFromPort": 80}, 'eq(vzEntry.dFromPort,"80")'),
    (
        vzEntry,
        {"dFromPort": [80, 443]},
        'or(eq(vzEntry.dFromPort,"80"),eq(vzEntry.dFromPort,"443"))',
    ),
    (vzEntry, {"dFromPort": between(80, 443)}, 'bw(vzEntry.dFromPort,"80","443")'),
    (vzEntry, {"stateful": True}, 'eq(vzEntry.stateful,"yes")'),
    # A dotted key is already qualified and passes through untouched.
    (fvBD, {"fvBD.arpFlood": True}, 'eq(fvBD.arpFlood,"yes")'),
    # The non-ambiguous wire names of the collision classes.
    (firmwareFwP, {"srVersion": "n9000-16.0(9c)"}, 'eq(firmwareFwP.srVersion,"n9000-16.0(9c)")'),
    (mdpProviderP, {"descr": "x"}, 'eq(mdpProviderP.descr,"x")'),
    # Catalogue-served classes, by string name.
    (
        "topSystem",
        {"enforceSubnetCheck": True, "controlPlaneMTU": 9000},
        'and(eq(topSystem.enforceSubnetCheck,"yes"),eq(topSystem.controlPlaneMTU,"9000"))',
    ),
    ("topSystem", {"address": "10.0.0.1"}, 'eq(topSystem.address,"10.0.0.1")'),
    (
        "faultInst",
        {"code": ["F0467", "F1394"]},
        'or(eq(faultInst.code,"F0467"),eq(faultInst.code,"F1394"))',
    ),
]


class TestWireNamesUnchanged:
    @pytest.mark.parametrize(
        ("cls", "kwargs", "expected"),
        _WIRE_REGRESSIONS,
        ids=[
            f"{c if isinstance(c, str) else c.__name__}-{'-'.join(k)}"
            for c, k, _ in _WIRE_REGRESSIONS
        ],
    )
    def test_wire_keyword_renders_as_today(
        self,
        make: _Factory,
        cls: type[ManagedObject] | str,
        kwargs: dict[str, FilterValue],
        expected: str,
    ) -> None:
        assert _where(make, cls, kwargs)[_FILTER] == expected

    @pytest.mark.parametrize(
        "cls_name",
        ["compHpNic", "compMgmtNic", "compSystem", "smarteventRuleInst", "smartfaultRuleInst"],
    )
    def test_wire_keyword_on_a_class_whose_readable_names_cannot_be_derived(
        self, make: _Factory, cls_name: str
    ) -> None:
        # The catalogue cannot derive readable names for these five classes
        # (``class_meta`` raises on an internal name collision); a wire keyword
        # must keep working on them exactly as today.
        assert _filter(make, cls_name, name="x") == f'eq({cls_name}.name,"x")'


# ── Unknown keys: today's behaviour (verbatim pass-through, no error) ─────────


class TestUnknownKeyUnchanged:
    def test_unknown_key_on_a_generated_class_passes_through(self, make: _Factory) -> None:
        assert _filter(make, fvBD, bogus=1) == 'eq(fvBD.bogus,"1")'

    def test_unknown_snake_key_passes_through(self, make: _Factory) -> None:
        assert _filter(make, fvBD, not_a_field=True) == 'eq(fvBD.not_a_field,"yes")'

    def test_unknown_key_on_a_catalogue_class_passes_through(self, make: _Factory) -> None:
        assert _filter(make, "topSystem", bogus="x") == 'eq(topSystem.bogus,"x")'

    def test_any_key_on_an_unknown_class_passes_through(self, make: _Factory) -> None:
        # A class neither generated nor in the catalogue: nothing to resolve
        # against, and no error either.
        assert _filter(make, "notAClass", bogus=1) == 'eq(notAClass.bogus,"1")'
        assert _filter(make, "notAClass", arp_flooding=True) == ('eq(notAClass.arp_flooding,"yes")')

    def test_unknown_key_with_wildcard_and_membership(self, make: _Factory) -> None:
        assert _filter(make, fvBD, bogus="a*") == 'wcard(fvBD.bogus,"a*")'
        assert _filter(make, fvBD, bogus=["a", "b"]) == 'or(eq(fvBD.bogus,"a"),eq(fvBD.bogus,"b"))'


# ── Collisions: fail loud ─────────────────────────────────────────────────────


_COLLISIONS: list[tuple[type[ManagedObject], str, str, str]] = [
    # (class, ambiguous key, wire it would be as a readable name, readable of the key-as-wire)
    (firmwareFwP, "version", "srVersion", "target_firmware_version"),
    (firmwareCatFwP, "version", "srVersion", "target_firmware_version"),
    (firmwareCtrlrFwP, "version", "srVersion", "target_firmware_version"),
    (mdpProviderP, "description", "descr", "multi_domain_profile_description"),
]


class TestCollisionFailsLoud:
    def test_the_collision_list_is_real(self) -> None:
        # Guard the premise: each key is the wire name of one field and the
        # readable name of another field of the same class.
        for cls, key, other_wire, key_readable in _COLLISIONS:
            fields = cls.model_fields
            assert fields[key].validation_alias == other_wire
            assert fields[key_readable].validation_alias == key

    @pytest.mark.parametrize(
        ("cls", "key", "other_wire", "key_readable"),
        _COLLISIONS,
        ids=[f"{c.__name__}.{k}" for c, k, _, _ in _COLLISIONS],
    )
    def test_ambiguous_key_raises_at_where(
        self,
        make: _Factory,
        cls: type[ManagedObject],
        key: str,
        other_wire: str,
        key_readable: str,
    ) -> None:
        with pytest.raises(ValueError) as info:
            make(cls).where(**{key: "x"})
        message = str(info.value)
        # Names both readings…
        assert key in message
        assert other_wire in message
        # …and points to the qualified explicit form.
        assert "eq(" in message
        assert f"{cls.__name__}." in message

    @pytest.mark.parametrize("value", ["x", ["x", "y"], "x*", any_of("x"), like("x")])
    def test_ambiguous_key_raises_whatever_the_value_shape(
        self, make: _Factory, value: FilterValue
    ) -> None:
        with pytest.raises(ValueError, match="srVersion"):
            make(firmwareFwP).where(version=value)

    def test_ambiguous_key_raises_alongside_valid_keys(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="srVersion"):
            make(firmwareFwP).where(name="fw", version="x")

    def test_each_side_is_reachable_without_ambiguity(self, make: _Factory) -> None:
        # The wire ``srVersion`` and the readable ``target_firmware_version``
        # name one property each, unambiguously.
        assert _filter(make, firmwareFwP, srVersion="a") == 'eq(firmwareFwP.srVersion,"a")'
        assert _filter(make, firmwareFwP, target_firmware_version="a") == (
            'eq(firmwareFwP.version,"a")'
        )
        assert _filter(make, mdpProviderP, multi_domain_profile_description="a") == (
            'eq(mdpProviderP.description,"a")'
        )

    def test_the_qualified_escape_hatch_works(self, make: _Factory) -> None:
        q = make(firmwareFwP).where(eq("firmwareFwP.version", "a"))
        assert q.build()[1][_FILTER] == 'eq(firmwareFwP.version,"a")'
        q = make(firmwareFwP).where(eq("version", "a", cls_name="firmwareFwP"))
        assert q.build()[1][_FILTER] == 'eq(firmwareFwP.version,"a")'
        q = make(firmwareFwP).where(eq("firmwareFwP.srVersion", "a"))
        assert q.build()[1][_FILTER] == 'eq(firmwareFwP.srVersion,"a")'

    def test_ambiguous_key_through_the_string_class_name(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="srVersion"):
            make("firmwareFwP").where(version="x")


# ── Catalogue-served classes (queried by string name) ─────────────────────────


_SYNTHETIC = "zzReadableSynthetic"


@pytest.fixture
def synthetic_collision(monkeypatch: pytest.MonkeyPatch) -> str:
    """Plant a catalogue class with a readable/wire collision; return its name.

    No class the catalogue serves without a model has such a collision (all
    ~15 000 scanned), so the rule is pinned on a synthetic entry planted in the
    catalogue's memo cache — the entry ``class_meta`` returns before it ever
    reads the ``.db``.  Wire ``alpha`` reads as ``gamma``; wire ``beta`` reads
    as ``alpha``: the key ``alpha`` is ambiguous.
    """
    meta = ClassMeta(
        class_name=_SYNTHETIC,
        readable_to_wire={"gamma": "alpha", "alpha": "beta"},
        wire_to_readable={"alpha": "gamma", "beta": "alpha"},
        wire_to_kind={"alpha": "str", "beta": "str"},
        naming=frozenset(),
        is_stat=False,
    )
    monkeypatch.setitem(catalog()._meta, _SYNTHETIC, meta)
    return _SYNTHETIC


class TestCatalogueClass:
    def test_premise_topSystem_has_no_model_and_renamed_props(self) -> None:
        meta = catalog().class_meta("topSystem")
        assert not meta.has_model
        assert meta.wire_to_readable["enforceSubnetCheck"] == "enforce_subnet_check"
        assert meta.wire_to_readable["controlPlaneMTU"] == "control_plane_mtu"
        assert meta.wire_to_readable["address"] == "infrastructure_ip"

    def test_readable_bool(self, make: _Factory) -> None:
        got = _filter(make, "topSystem", enforce_subnet_check=True)
        assert got == 'eq(topSystem.enforceSubnetCheck,"yes")'
        assert got == _filter(make, "topSystem", enforceSubnetCheck=True)

    def test_readable_int(self, make: _Factory) -> None:
        got = _filter(make, "topSystem", control_plane_mtu=9000)
        assert got == 'eq(topSystem.controlPlaneMTU,"9000")'

    def test_readable_name_far_from_its_wire_name(self, make: _Factory) -> None:
        got = _filter(make, "topSystem", infrastructure_ip="10.0.0.1")
        assert got == 'eq(topSystem.address,"10.0.0.1")'

    def test_readable_and_wire_mixed_is_byte_equal(self, make: _Factory) -> None:
        readable = _filter(make, "topSystem", enforce_subnet_check=True, control_plane_mtu=9000)
        wire = _filter(make, "topSystem", enforceSubnetCheck=True, controlPlaneMTU=9000)
        assert readable == wire

    def test_readable_with_smart_kwargs(self, make: _Factory) -> None:
        assert _filter(make, "eqptSensor", major_threshold=[70, 80]) == (
            'or(eq(eqptSensor.majorThresh,"70"),eq(eqptSensor.majorThresh,"80"))'
        )
        assert _filter(make, "lldpAdjEp", management_address="10.*") == (
            'wcard(lldpAdjEp.mgmtIp,"10.*")'
        )

    def test_synthetic_collision_raises(self, make: _Factory, synthetic_collision: str) -> None:
        with pytest.raises(ValueError) as info:
            make(synthetic_collision).where(alpha="x")
        message = str(info.value)
        assert "alpha" in message
        assert "beta" in message
        assert "eq(" in message

    def test_synthetic_sides_resolve(self, make: _Factory, synthetic_collision: str) -> None:
        assert _filter(make, synthetic_collision, gamma="x") == f'eq({_SYNTHETIC}.alpha,"x")'
        assert _filter(make, synthetic_collision, beta="x") == f'eq({_SYNTHETIC}.beta,"x")'


# ── subtree_where: the same rule, against the INCLUDED class ──────────────────


def _subtree_filter(q: _AnyQuery) -> str:
    return q.build()[1][_SUBTREE_FILTER]


class TestSubtreeWhere:
    def test_readable_name_of_the_included_class(self, make: _Factory) -> None:
        readable = make(fvBD).include(fvSubnet).subtree_where(subnet_control={"querier"})
        wire = make(fvBD).include(fvSubnet).subtree_where(ctrl={"querier"})
        assert _subtree_filter(readable) == 'eq(fvSubnet.ctrl,"querier")'
        assert readable.build() == wire.build()

    def test_readable_name_of_the_query_class_is_not_translated(self, make: _Factory) -> None:
        # ``arp_flooding`` is a field of fvBD (the query class), not of
        # fvSubnet (the included class): unknown there, so it passes through.
        q = make(fvBD).include(fvSubnet).subtree_where(arp_flooding=True)
        assert _subtree_filter(q) == 'eq(fvSubnet.arp_flooding,"yes")'

    def test_collision_is_judged_against_the_included_class(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="srVersion"):
            make(fvBD).include(firmwareFwP).subtree_where(version="x")

    def test_no_collision_when_only_the_query_class_has_one(self, make: _Factory) -> None:
        q = make(firmwareFwP).include(fvSubnet).subtree_where(version="x")
        assert _subtree_filter(q) == 'eq(fvSubnet.version,"x")'

    def test_query_where_and_subtree_where_resolve_independently(self, make: _Factory) -> None:
        q = (
            make(fvBD)
            .where(arp_flooding=True)
            .include(fvSubnet)
            .subtree_where(display_name="dmz-*")
        )
        params = q.build()[1]
        assert params[_FILTER] == 'eq(fvBD.arpFlood,"yes")'
        assert params[_SUBTREE_FILTER] == 'wcard(fvSubnet.nameAlias,"dmz-*")'

    def test_included_catalogue_class(self, make: _Factory) -> None:
        q = make("eqptCh").include("eqptSensor").subtree_where(major_threshold=70)
        assert _subtree_filter(q) == 'eq(eqptSensor.majorThresh,"70")'

    def test_wire_name_unchanged(self, make: _Factory) -> None:
        q = make(fvBD).include(fvSubnet).subtree_where(ip="10.0.0.1/24")
        assert _subtree_filter(q) == 'eq(fvSubnet.ip,"10.0.0.1/24")'

    def test_unknown_key_unchanged(self, make: _Factory) -> None:
        q = make(fvBD).include(fvSubnet).subtree_where(bogus="x")
        assert _subtree_filter(q) == 'eq(fvSubnet.bogus,"x")'

    def test_keyword_without_include_still_raises(self, make: _Factory) -> None:
        with pytest.raises(ValueError, match="include"):
            make(fvBD).subtree_where(subnet_control={"querier"})


# ── Explicit FilterExpr arguments are untouched ───────────────────────────────


class TestExplicitExpressionsUntouched:
    def test_readable_looking_qualified_property_is_verbatim(self, make: _Factory) -> None:
        q = make(fvBD).where(eq("fvBD.arp_flooding", True))
        assert q.build()[1][_FILTER] == 'eq(fvBD.arp_flooding,"yes")'

    def test_cls_name_form_is_verbatim(self, make: _Factory) -> None:
        q = make(fvBD).where(eq("arp_flooding", True, cls_name="fvBD"))
        assert q.build()[1][_FILTER] == 'eq(fvBD.arp_flooding,"yes")'

    def test_expression_and_readable_keyword_combine(self, make: _Factory) -> None:
        q = make(fvBD).where(wcard("fvBD.name", "prod-*"), arp_flooding=True)
        assert q.build()[1][_FILTER] == ('and(wcard(fvBD.name,"prod-*"),eq(fvBD.arpFlood,"yes"))')

    def test_subtree_expression_is_verbatim(self, make: _Factory) -> None:
        q = make(fvBD).include(fvSubnet).subtree_where(eq("fvSubnet.subnet_control", "querier"))
        assert _subtree_filter(q) == 'eq(fvSubnet.subnet_control,"querier")'
