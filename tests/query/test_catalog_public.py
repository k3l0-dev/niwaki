"""The public discovery surface, ``niwaki.catalog`` — a thin door to the reader.

These assert the public functions delegate to the lazily-opened catalogue and
re-export its result types; the reader itself is tested in ``test_catalog.py``.
They need the catalogue ``.db`` built from the raw schemas, so they skip when the
corpus is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from niwaki import catalog
from niwaki._codegen import generate_catalog as gc
from niwaki.query import _catalog

CORPUS_PRESENT = gc.SCHEMA_DIR.is_dir()
needs_corpus = pytest.mark.skipif(
    not CORPUS_PRESENT, reason="raw APIC schemas (data/schemas) not present"
)


def test_public_types_are_the_readers_types() -> None:
    assert catalog.ClassDoc is _catalog.ClassDoc
    assert catalog.PropDoc is _catalog.PropDoc
    assert catalog.ClassMeta is _catalog.ClassMeta
    assert catalog.PropFlags is _catalog.PropFlags


@pytest.fixture(scope="module")
def catalog_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("cat") / "catalog.db"
    gc.build_catalog(out=out)
    return out


@pytest.fixture
def wired(catalog_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_catalog, "_instance", _catalog.Catalog(catalog_path))


@needs_corpus
def test_describe(wired: None) -> None:
    doc = catalog.describe("fvBD")
    assert doc.label == "Bridge Domain"
    assert any(p.readable == "arp_flooding" for p in doc.props)


@needs_corpus
def test_prop_meta(wired: None) -> None:
    assert catalog.prop_meta("fvBD", "arp_flooding").kind == "bool"


@needs_corpus
def test_search(wired: None) -> None:
    assert "fvBD" in catalog.search("bridge")


@needs_corpus
def test_find_prop(wired: None) -> None:
    assert ("fvBD", "arpFlood") in catalog.find_prop("arpFlood")


@needs_corpus
def test_concrete_subclasses(wired: None) -> None:
    assert "fvAEPg" in catalog.concrete_subclasses("fvEPg")


@needs_corpus
def test_class_meta(wired: None) -> None:
    meta = catalog.class_meta("fvBD")
    assert meta.readable_to_wire["arp_flooding"] == "arpFlood"


@needs_corpus
def test_fault_name(wired: None) -> None:
    assert catalog.fault_name("F2409") == "fltFvBDInvalidConfigOnBD"


@needs_corpus
def test_fault_name_unknown_code(wired: None) -> None:
    assert catalog.fault_name("F-nonexistent") is None


# ── generated_classes() / has_model — offline, no corpus needed ───────────────
# (they read the codegen's shipped index and the shipped catalogue, not a
# fixture build; the exhaustive cross-surface parity lives in
# tests/models/test_generated_parity.py)


def test_generated_classes_is_sorted_deduplicated_and_memoised() -> None:
    classes = catalog.generated_classes()
    assert isinstance(classes, tuple)
    assert list(classes) == sorted(set(classes))
    assert catalog.generated_classes() is classes  # computed once per process


def test_generated_classes_membership() -> None:
    classes = catalog.generated_classes()
    assert "fvBD" in classes  # configurable → generated model
    assert "faultInst" in classes  # the FaultCurrent exception still has a model
    assert "topSystem" not in classes  # readable only → catalogue-served
    assert "commTelnet" not in classes  # deprecated → no model


def test_has_model_agrees_with_generated_classes() -> None:
    classes = set(catalog.generated_classes())
    assert catalog.class_meta("fvBD").has_model is ("fvBD" in classes)
    assert catalog.class_meta("topSystem").has_model is False
    assert catalog.class_meta("lldpAdjEp").has_model is False  # learned, operational-only
    assert catalog.class_meta("faultThrValueDouble").has_model is True


# ── dn_formats() — offline, reads the shipped catalogue, no corpus needed ─────


def test_dn_formats_returns_the_single_template_of_a_simple_class() -> None:
    assert catalog.dn_formats("fvBD") == ("uni/tn-{name}/BD-{name}",)


def test_a_repeated_placeholder_is_legitimate() -> None:
    """Two ancestors identified by ``name`` produce ``{name}`` twice.

    Pinned because it reads like a bug and is not one: a consumer told to quote
    these verbatim must not "correct" it.
    """
    assert catalog.dn_formats("fvBD")[0].count("{name}") == 2


def test_one_class_can_live_in_many_places() -> None:
    """A subnet hangs off a bridge domain, an EPG, a tenant, an L2Out, and more.

    The whole point of the field: answering from the first template alone would
    be wrong eleven times out of twelve here.
    """
    templates = catalog.dn_formats("fvSubnet")
    assert len(templates) == 12
    assert "uni/tn-{name}/BD-{name}/subnet-[{ip}]" in templates
    assert "uni/tn-{name}/ap-{name}/epg-{name}/subnet-[{ip}]" in templates


def test_a_class_with_no_template_returns_empty() -> None:
    assert catalog.dn_formats("aaaADomainRef") == ()


def test_an_empty_string_is_a_template_not_an_empty_answer() -> None:
    """A container that prefixes nothing — distinct from "no templates at all".

    Six classes are shaped this way, five of them abstract; the root of the tree
    is only one of them. This is the value that breaks a consumer doing
    ``templates[0].split("/")`` or testing ``if t``.
    """
    from niwaki.query._catalog import catalog as reader

    con = reader()._connection
    all_empty = {
        name
        for (name,) in con.execute("SELECT class_name FROM mo")
        if catalog.dn_formats(name) == ("",)
    }
    assert all_empty == {
        "conditionSummary",
        "faultAThrValue",
        "frmwrkDeliveryCont",
        "pconsANodeDeployCtx",
        "topRoot",
        "vzACollectionDef",
    }


def test_an_empty_template_can_sit_beside_real_ones() -> None:
    """The shape that defeats indexing into the first element.

    One class mixes a prefix-less container in with four real templates, so
    "empty means the class has no places" is wrong twice over.
    """
    templates = catalog.dn_formats("frmwrkDeliveryDest")
    assert templates == ("oecont/", "pecont/", "emgrcont/", "cdcont/", "")
    assert len([t for t in templates if t]) == 4


def test_the_largest_list_is_not_truncated() -> None:
    """The tail of this distribution reaches five figures; nothing caps it."""
    assert len(catalog.dn_formats("faultDelegate")) == 64313


# ── rn_format() — offline, the inverse key for niwaki._dn.parse ───────────────


def test_rn_format_is_the_template_for_a_classs_own_segment() -> None:
    assert catalog.rn_format("fvBD") == "BD-{name}"
    assert catalog.rn_format("fvSubnet") == "subnet-[{ip}]"


def test_a_relation_with_a_fixed_rn_has_no_placeholder() -> None:
    assert catalog.rn_format("fvRsCtx") == "rsctx"


def test_a_class_with_no_rn_format_returns_empty() -> None:
    """Abstract classes and the tree root stand for no segment of their own."""
    assert catalog.rn_format("topRoot") == ""


def test_rn_format_of_an_unknown_class_raises() -> None:
    from niwaki.exceptions import UnknownClassError

    with pytest.raises(UnknownClassError):
        catalog.rn_format("fvNoSuchThing")


# ── prop_flags() — offline, the raw material of data-driven normalisation ─────


def test_prop_flags_of_an_ordinary_configurable_property() -> None:
    flags = catalog.prop_flags("fvBD")["arpFlood"]
    assert flags.is_configurable and flags.read_write
    assert not flags.read_only and not flags.is_naming and not flags.secure


def test_prop_flags_of_an_operational_property() -> None:
    """What the controller computes: readOnly + implicit, never configurable."""
    flags = catalog.prop_flags("topSystem")["currentTime"]
    assert flags.read_only and flags.implicit
    assert not flags.is_configurable


def test_prop_flags_of_a_secret() -> None:
    """An SNMP auth key: configurable, write-once, never echoed back."""
    flags = catalog.prop_flags("snmpUserP")["authKey"]
    assert flags.secure and flags.create_only and flags.is_configurable


def test_the_secure_flag_is_not_a_secret_policy() -> None:
    """The trap that mandates a *curated* secret policy on top of the flags.

    The SNMP community string is a NAMING property — it rides inside the DN
    and carries no ``secure`` flag; a device credential value is plain
    read-write.  Pinned so nobody ever ships a redaction built on ``secure``
    alone.
    """
    community = catalog.prop_flags("snmpCommunityP")["name"]
    assert community.is_naming and not community.secure
    credential = catalog.prop_flags("vnsCCred")["value"]
    assert credential.read_write and not credential.secure


def test_prop_flags_agrees_with_class_meta_on_naming() -> None:
    """Two unpackings of the same bits must never diverge."""
    flags = catalog.prop_flags("fvSubnet")
    naming_from_flags = {wire for wire, f in flags.items() if f.is_naming}
    assert naming_from_flags == set(catalog.class_meta("fvSubnet").naming)


def test_prop_flags_is_memoised_per_class() -> None:
    assert catalog.prop_flags("fvBD") is catalog.prop_flags("fvBD")


def test_prop_flags_of_an_unknown_class_raises() -> None:
    from niwaki.exceptions import UnknownClassError

    with pytest.raises(UnknownClassError):
        catalog.prop_flags("fvNoSuchThing")


def test_rn_format_pairs_with_dn_formats() -> None:
    """The RN format is always the last segment of every DN format it appears in.

    This is the invariant :func:`niwaki._dn.parse` leans on: a DN read back
    ends in this class's RN, whatever the parent path in front of it.
    """
    from niwaki._dn import rn_of

    rn_format = catalog.rn_format("fvSubnet")
    for dn_format in catalog.dn_formats("fvSubnet"):
        assert rn_of(dn_format) == rn_format


def test_an_unknown_class_raises() -> None:
    from niwaki.exceptions import UnknownClassError

    with pytest.raises(UnknownClassError):
        catalog.dn_formats("definitelyNotAnAciClass")
    # Also a KeyError, like every other catalogue lookup.
    with pytest.raises(KeyError):
        catalog.dn_formats("definitelyNotAnAciClass")


def test_the_result_is_a_fresh_tuple_each_call() -> None:
    """Recomputed, not memoised — the decision the reader documents.

    The largest class carries 64,313 templates; pinning that in the reader
    would hold megabytes for the life of the process on a path few callers walk
    twice.  Identity, not equality: equality holds either way and would let a
    cache slip in unnoticed.
    """
    first = catalog.dn_formats("fvSubnet")
    second = catalog.dn_formats("fvSubnet")
    assert isinstance(first, tuple)
    assert first == second
    assert first is not second


def test_concurrent_readers_never_receive_another_class_rows() -> None:
    """The catalogue is documented as safe to share; this holds it to it.

    One connection is shared across threads, and the driver caches prepared
    statements per connection keyed by SQL text.  Without ``cached_statements=0``
    two threads running this same query trade one statement object and read each
    other's rows — silently, with no error: measured on the shipped catalogue,
    a lookup for ``fvBD`` came back holding ``fvSubnet``'s templates.
    """
    import threading

    expected = {name: catalog.dn_formats(name) for name in ("fvBD", "fvSubnet")}
    wrong: list[tuple[str, tuple[str, ...]]] = []
    failures: list[str] = []
    guard = threading.Lock()

    def hammer(name: str) -> None:
        for _ in range(400):
            try:
                got = catalog.dn_formats(name)
            except Exception as exc:
                with guard:
                    failures.append(type(exc).__name__)
                continue
            if got != expected[name]:
                with guard:
                    wrong.append((name, got))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in ("fvBD", "fvSubnet") * 3]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert wrong == [], f"{len(wrong)} reader(s) received another class's templates"
    assert failures == [], f"concurrent readers raised: {set(failures)}"
