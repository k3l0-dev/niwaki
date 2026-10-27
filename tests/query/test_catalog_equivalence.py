"""The read catalogue answers exactly as it did before ``prop`` was normalised.

The shipped ``catalog.db`` stores one ``prop`` row per (class, property):
332,297 rows that carry only 25,901 distinct signatures.  Normalising that
table into shared signatures changes the storage layout under every reader
query that touches a property.  This module freezes the reader's answers on
the **shipped** db *before* that change, so the change has to reproduce them
bit for bit:

- :meth:`Catalog.class_meta` and :meth:`Catalog.prop_flags` on **every** class
  in the db, as one SHA-256 each;
- :meth:`Catalog.describe` and :meth:`Catalog.dn_formats` on a sample (every
  7th class, plus the classes whose shapes a stride does not guarantee);
- :meth:`Catalog.find_prop`, :meth:`Catalog.search`, :meth:`Catalog.fault_name`,
  :meth:`Catalog.concrete_subclasses` and :meth:`Catalog._rn_format_rows`.

The schema-wide private sets (``_secure_props``, ``_exportable_classes``,
``_concrete_classes``, ``_never_creatable_classes``) are already frozen on the
shipped db by ``tests/test_single_catalog_opener.py``
(``TestReaderAnswersMatchTheGoldens``) and are not repeated here.

**Corpus-free.**  Every golden is computed from the committed ``catalog.db``,
never from the raw schemas, so the public CI runs this module too.

**Canonical form.**  Each answer is serialised to compact, key-sorted JSON;
mappings are hashed as sorted ``(key, value)`` pairs so the hash pins
*content*.  The iteration order of :class:`ClassMeta`'s mappings is pinned
separately (:func:`test_class_meta_iterates_in_wire_name_order`), so a
failure says which of the two moved.  Lists whose order the reader defines
(``describe`` properties, ``find_prop`` and ``search`` results) are hashed in
the order returned.

**Cost.**  Walking all 15,452 classes through ``class_meta`` and
``prop_flags`` dominates: about 4 s each on a loaded developer machine (the
whole module runs in roughly 10 s).  That is the price of an exhaustive pin
on a storage rewrite; a sample would miss the one class that diverges.

Frozen on the shipped catalogue (APIC 6.0(9c)) on 2026-10-27, before the
normalisation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator

import pytest

from niwaki.query import _catalog

# ── Frozen goldens ────────────────────────────────────────────────────────────

CLASS_COUNT = 15452

#: The five classes whose readable names collide: ``class_meta`` refuses them
#: today (``ValueError``) rather than silently dropping a property.  The
#: refusal is part of the answer and must survive the rewrite unchanged.
NAME_COLLISION_CLASSES = (
    "compHpNic",
    "compMgmtNic",
    "compSystem",
    "smarteventRuleInst",
    "smartfaultRuleInst",
)

CLASS_META_SHA256 = "5bc9844f4547bb8ccd66a97e28172913ebe42829839a4290fde9cfbcdc3e072f"
#: ``list(wire_to_readable)`` per class, in iteration order (collisions excluded).
WIRE_TO_READABLE_ORDER_SHA256 = "547a3412c6412d77c08bd3480eb1adc19135ab99018ac02548ab4066b1092a7c"
PROP_FLAGS_SHA256 = "da4c9bdb41a9a0a0be95a617ff6e162bd492df94fbedbba75448de0cf27b89de"

#: Classes whose shapes the 1-in-7 stride does not guarantee: the hot
#: examples of the docs, a fault and a hardware class, and the three shapes of
#: ``dnFormats`` (none at all, the one-empty-string root, the largest list).
DESCRIBE_ANCHORS = (
    "fvBD",
    "fvCEp",
    "faultInst",
    "topSystem",
    "aaaADomainRef",
    "topRoot",
    "faultDelegate",
)
DESCRIBE_SAMPLE_LEN = 2213
DESCRIBE_SHA256 = "657db040d9d0af2680d804deffe6ef95f89693582a7d23c482f21413bf268499"
DN_FORMATS_SHA256 = "ab259b24fdba423fa4c1842e534b376f12e5a47ff381960ab3c7c6108cb1cc13"

RN_FORMAT_ROWS_LEN = 13497
RN_FORMAT_ROWS_SHA256 = "3101cea33cbb9699ca1d0bc999bc454a4f68bf7b5709990f0a05fcd1ca74d8fe"

#: ``find_prop(term)`` -> (len, sha256) at the default limit, then unlimited.
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
FIND_PROP: dict[str, tuple[tuple[int, str], tuple[int, str]]] = {
    "mac": (
        (50, "79c91dca242de74e09bd0e10c9268e7a332c470e9e468de6c5ba6741d3f71069"),
        (3748, "e4d4befac43eb4490370282cdf8fdd80c54db9561f8d7bb2136c211b7df9afec"),
    ),
    "addr": (
        (50, "3a2981bfaaf0b8a18d3d3a029322f5321902e601f0b34b16fd44b1cc59cdcf60"),
        (2023, "db0d072aeda4e4d731a7a4af27d9ce459de464dced9506bc77a243470a66b1c1"),
    ),
    "dscp": (
        (50, "2f73a1c50ffa5ca67ef746a60859dbe047a4f20d8d392df18d70a7d5b80668b3"),
        (113, "9b399ddbaba1f21faeba08459c9d05f72704cae09ac6c81c06139d4782bd927d"),
    ),
    "zzz-none": ((0, _EMPTY_SHA256), (0, _EMPTY_SHA256)),
}

#: ``search(term)`` -> (len, sha256) at the default limit, in FTS5 rank order
#: (the ``search_doc``/FTS tables are untouched by the normalisation; frozen
#: anyway, since the reader's discovery surface moves as one).
SEARCH: dict[str, tuple[int, str]] = {
    "bridge": (50, "fa27ce0b8b175a8982812ec0021fb5be23c9a14a673f6f17aed75789fe3bf729"),
    "endpoint": (50, "d360728775f74d49642deedf229da899ee74f37773a6475addd29ed4613c7807"),
    "fault": (41, "52d5786ad56b7e37feda047629eda711ab623e7a48f2b791916952fee2c0eca6"),
    "tenant": (50, "78b9987c1207518b5bfa52d187bb5a026f313e7542574041db7287f5ff072fb7"),
    "zzz-none": (0, _EMPTY_SHA256),
}

#: 17 codes on a stride across the 1,491 distinct codes, the docs' example,
#: and two unknown codes.
FAULT_NAMES: dict[str, str | None] = {
    "F0020": "fltFabricSelectorIssuesConfig-failed",
    "F0618": "fltMgmtNodeDefMgmtAddressNotAllocated",
    "F0925": "fltFabricRsPodPGrpResolveFail",
    "F1054": "fltSyntheticRsPolicyResolveFail",
    "F1240": "fltVzTabooConfigurationFailed",
    "F1487": "fltInfraRsOverrideMcpIfPolResolveFail",
    "F1696": "fltL1PhysIfPortEPGSpanInvalid",
    "F2008": "fltDppClassOperStFailed",
    "F2459": "fltHsrpRsGroupPolResolveFail",
    "F2694": "fltOpflexIDEpOperationalIssues",
    "F2915": "fltCoppIfInstallationFailedInvalidRateBurst",
    "F3113": "fltFcpmFcotBaseInvalidTransceiver",
    "F3306": "fltPcFcAggrMbrIfPortSuspended",
    "F3571": "fltBgpRsPeerToProfileResolveFail",
    "F3866": (
        "fltHcloudSharedResourceToHubCtxCreateOperCloudSharedResourceToHubCtxCreateOperStateDown"
    ),
    "F4125": "fltFvSubnetConfigurationFailed",
    "F4304": "fltPimInterVRFEntryPolConfIssue",
    "F0467": "fltFvNwIssuesConfig-failed",
    "F9999999": None,
    "E-unknown": None,
}

FVEPG_CONCRETE_SUBCLASSES = [
    "dhcpCRelPg",
    "dhcpPRelPg",
    "fvAEPg",
    "fvESg",
    "fvTnlEPg",
    "infraCEPg",
    "infraPEPg",
    "l2extInstP",
    "l3extInstP",
    "l3extInstPDef",
    "mgmtInB",
    "vnsEPpInfo",
    "vnsREPpInfo",
    "vnsSDEPpInfo",
    "vnsSHEPpInfo",
]


# ── Canonical form ────────────────────────────────────────────────────────────


def _canon(value: object) -> str:
    """Compact, key-sorted JSON — one deterministic line per answer."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(lines: Iterable[str]) -> str:
    """SHA-256 of the lines, newline-joined, in the order given."""
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _class_meta_line(cat: _catalog.Catalog, name: str) -> str:
    """One class's ``class_meta`` answer — or its refusal — as a canonical line."""
    try:
        meta = cat.class_meta(name)
    except ValueError as exc:
        return _canon([name, "ValueError", str(exc)])
    return _canon(
        [
            name,
            sorted(meta.readable_to_wire.items()),
            sorted(meta.wire_to_readable.items()),
            sorted(meta.wire_to_kind.items(), key=lambda kv: kv[0]),
            sorted(meta.naming),
            meta.is_stat,
        ]
    )


def _prop_flags_line(cat: _catalog.Catalog, name: str) -> str:
    """One class's ``prop_flags``: each wire name with its 14 booleans in field order."""
    flags = cat.prop_flags(name)
    return _canon([name, sorted((wire, dataclasses.astuple(f)) for wire, f in flags.items())])


def _describe_line(cat: _catalog.Catalog, name: str) -> str:
    """One class's full ``describe`` (property order as returned), or its refusal."""
    try:
        doc = cat.describe(name)
    except ValueError as exc:
        return _canon([name, "ValueError", str(exc)])
    return _canon([name, dataclasses.asdict(doc)])


def _pairs(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """``(class, prop)`` pairs as ``class\\tprop`` lines (tab: in neither name)."""
    return [f"{cls}\t{prop}" for cls, prop in pairs]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def shipped() -> Iterator[_catalog.Catalog]:
    """A fresh reader on the committed db — no memo shared with other modules."""
    cat = _catalog.Catalog(_catalog.DEFAULT_PATH)
    yield cat
    cat.close()


@pytest.fixture(scope="module")
def class_names() -> list[str]:
    """Every class in the shipped db, in table order.

    Read from the ``mo`` table directly: the reader has no "list every class"
    method, and ``mo`` is untouched by the property normalisation.
    """
    con = sqlite3.connect(f"file:{_catalog.DEFAULT_PATH}?mode=ro", uri=True)
    try:
        return [str(n) for (n,) in con.execute("SELECT class_name FROM mo ORDER BY id")]
    finally:
        con.close()


@pytest.fixture(scope="module")
def sample(class_names: list[str]) -> list[str]:
    """Every 7th class plus :data:`DESCRIBE_ANCHORS`, sorted."""
    return sorted(set(class_names[::7]) | set(DESCRIBE_ANCHORS))


# ── class_meta / prop_flags: every class ──────────────────────────────────────


def test_class_universe(class_names: list[str]) -> None:
    assert len(class_names) == CLASS_COUNT
    assert len(set(class_names)) == CLASS_COUNT
    assert set(DESCRIBE_ANCHORS) <= set(class_names)


def test_class_meta_every_class(shipped: _catalog.Catalog, class_names: list[str]) -> None:
    got = _sha(_class_meta_line(shipped, name) for name in sorted(class_names))
    assert got == CLASS_META_SHA256


@pytest.mark.parametrize("name", NAME_COLLISION_CLASSES)
def test_class_meta_still_refuses_colliding_names(shipped: _catalog.Catalog, name: str) -> None:
    with pytest.raises(ValueError, match=rf"^{name}: Python name collision"):
        shipped.class_meta(name)


def test_class_meta_spot_checks(shipped: _catalog.Catalog) -> None:
    """Readable anchors beside the hash — a failure names a culprit."""
    bd = shipped.class_meta("fvBD")
    assert bd.readable_to_wire["arp_flooding"] == "arpFlood"
    assert bd.wire_to_readable["arpFlood"] == "arp_flooding"
    assert bd.wire_to_kind["arpFlood"] == "bool"
    assert bd.naming == frozenset({"name"})
    assert bd.is_stat is False
    assert shipped.class_meta("eqptEgrBytes5min").is_stat is True


def test_class_meta_iterates_in_wire_name_order(
    shipped: _catalog.Catalog, class_names: list[str]
) -> None:
    """``ClassMeta`` is public: the order of its mappings is part of the answer.

    Today the reader receives a class's properties in wire-name order — the
    ``prop`` table's primary key is ``(class_id, wire_name)`` — so
    ``wire_to_kind`` and ``prop_flags`` iterate in that order.  A signature
    table keyed otherwise would reorder them silently unless the reader asks
    for the order.  ``wire_to_readable`` is the naming resolver's output on
    that ordered input (a collision fallback moves to the end), so its
    iteration order is pinned by hash; ``readable_to_wire`` mirrors it.
    """
    out_of_order: list[str] = []
    order_lines: list[str] = []
    for name in sorted(class_names):
        if name in NAME_COLLISION_CLASSES:
            continue
        meta = shipped.class_meta(name)
        wires = list(meta.wire_to_kind)
        if (
            wires != sorted(wires)
            or list(shipped.prop_flags(name)) != wires
            or list(meta.readable_to_wire.items())
            != [(py, wire) for wire, py in meta.wire_to_readable.items()]
        ):
            out_of_order.append(name)
        order_lines.append(_canon([name, list(meta.wire_to_readable)]))
    assert out_of_order == [], f"{len(out_of_order)} class(es), e.g. {out_of_order[:5]}"
    assert _sha(order_lines) == WIRE_TO_READABLE_ORDER_SHA256


def test_prop_flags_every_class(shipped: _catalog.Catalog, class_names: list[str]) -> None:
    got = _sha(_prop_flags_line(shipped, name) for name in sorted(class_names))
    assert got == PROP_FLAGS_SHA256


def test_prop_flags_spot_checks(shipped: _catalog.Catalog) -> None:
    pwd = shipped.prop_flags("aaaUser")["pwd"]
    assert pwd.secure is True
    assert pwd.is_configurable is True
    name = shipped.prop_flags("fvBD")["name"]
    assert name.is_naming is True
    assert name.secure is False


# ── describe / dn_formats: a sample ───────────────────────────────────────────


def test_describe_sample(shipped: _catalog.Catalog, sample: list[str]) -> None:
    assert len(sample) == DESCRIBE_SAMPLE_LEN
    assert _sha(_describe_line(shipped, name) for name in sample) == DESCRIBE_SHA256


def test_describe_spot_checks(shipped: _catalog.Catalog) -> None:
    doc = shipped.describe("fvBD")
    assert doc.label == "Bridge Domain"
    assert len(doc.props) == 45
    wires = [p.wire for p in doc.props]
    # Binary collation: upper case sorts before lower case.
    assert wires[:3] == ["OptimizeWanBandwidth", "annotation", "arpFlood"]
    arp = next(p for p in doc.props if p.wire == "arpFlood")
    assert (arp.readable, arp.kind, arp.default) == ("arp_flooding", "bool", "no")
    assert arp.enum_values  # the yes/no members come through the enum store


def test_dn_formats_sample(shipped: _catalog.Catalog, sample: list[str]) -> None:
    got = _sha(_canon([name, list(shipped.dn_formats(name))]) for name in sample)
    assert got == DN_FORMATS_SHA256


def test_dn_formats_spot_checks(shipped: _catalog.Catalog) -> None:
    assert shipped.dn_formats("fvBD") == ("uni/tn-{name}/BD-{name}",)
    assert shipped.dn_formats("aaaADomainRef") == ()
    assert shipped.dn_formats("topRoot") == ("",)
    assert len(shipped.dn_formats("faultDelegate")) == 64313


def test_rn_format_rows(shipped: _catalog.Catalog) -> None:
    rows = shipped._rn_format_rows()  # pyright: ignore[reportPrivateUsage]
    assert len(rows) == RN_FORMAT_ROWS_LEN
    assert _sha(sorted(_pairs(rows))) == RN_FORMAT_ROWS_SHA256


# ── Discovery: find_prop, search, fault_name, concrete_subclasses ─────────────


@pytest.mark.parametrize("term", ["mac", "addr", "dscp", "zzz-none"])
def test_find_prop(shipped: _catalog.Catalog, term: str) -> None:
    (want_len, want_sha), (want_all_len, want_all_sha) = FIND_PROP[term]
    first = shipped.find_prop(term)
    assert (len(first), _sha(_pairs(first))) == (want_len, want_sha)
    every = shipped.find_prop(term, limit=1_000_000)
    assert (len(every), _sha(_pairs(every))) == (want_all_len, want_all_sha)


def test_find_prop_spot_checks(shipped: _catalog.Catalog) -> None:
    assert shipped.find_prop("mac")[:3] == [
        ("aaaApplianceCertRes", "hmac"),
        ("aaaCertGenReq", "hmac"),
        ("aaaCertGenReq", "podmac"),
    ]
    assert shipped.find_prop("zzz-none") == []


@pytest.mark.parametrize("term", ["bridge", "endpoint", "fault", "tenant", "zzz-none"])
def test_search(shipped: _catalog.Catalog, term: str) -> None:
    if not shipped._fts_available():  # pyright: ignore[reportPrivateUsage]
        pytest.skip("this sqlite build lacks FTS5; the goldens are the ranked FTS answers")
    got = shipped.search(term)
    assert (len(got), _sha(got)) == SEARCH[term]


def test_fault_name(shipped: _catalog.Catalog) -> None:
    assert len(FAULT_NAMES) == 20
    assert {code: shipped.fault_name(code) for code in FAULT_NAMES} == FAULT_NAMES


def test_concrete_subclasses_of_fvepg(shipped: _catalog.Catalog) -> None:
    assert shipped.concrete_subclasses("fvEPg") == FVEPG_CONCRETE_SUBCLASSES
