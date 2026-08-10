"""The curated secret policy — witnesses, and the drift guard that closes it.

The policy's value is exhaustiveness: every property the sweep patterns raise
against the shipped catalogue is either flagged ``secure`` by the schema,
curated as secret, or dismissed with a written reason.  The drift guard
recomputes the sweep here, so a future train's new password-shaped property
breaks the build until a human triages it — the same discipline as the
curation coverage audit.
"""

from __future__ import annotations

import sqlite3

import pytest

from niwaki import _secrets
from niwaki._secrets import (
    _ASYM_EXCLUDE,
    _ASYM_VALUE,
    CLASS_PATTERN,
    DISMISS_RULES,
    PROP_PATTERN,
    REVIEWED_NOT_SECRET,
    SECRET_DN_POSITIONS,
    SECRET_VALUE_POSITIONS,
    VALUE_PROPS,
    is_secret_prop,
    secret_dn_classes,
)
from niwaki.query import _catalog


def _catalog_rows() -> list[tuple[str, str, bool, bool, bool]]:
    """(class, prop, is_naming, is_configurable, is_secure) for every prop."""
    con = sqlite3.connect(f"file:{_catalog.DEFAULT_PATH}?mode=ro", uri=True)
    try:
        (raw,) = con.execute("SELECT value FROM manifest WHERE key='prop_flags'").fetchone()
        order = str(raw).split(",")
        secure = 1 << order.index("secure")
        naming = 1 << order.index("isNaming")
        config = 1 << order.index("isConfigurable")
        return [
            (cls, wire, bool(flags & naming), bool(flags & config), bool(flags & secure))
            for cls, wire, flags in con.execute(
                "SELECT m.class_name, p.wire_name, p.flags FROM prop p JOIN mo m ON p.class_id=m.id"
            )
        ]
    finally:
        con.close()


def _classes_with_a_secure_prop() -> set[str]:
    return {cls for cls, _w, _n, _c, is_secure in _catalog_rows() if is_secure}


def _sweep() -> set[tuple[str, str]]:
    """Every candidate the three nets raise (name pattern, class pattern, and
    the secure-sibling asymmetry).  Secure-flagged props are included — the
    triage treats them as handled by the mechanical layer, but they must ride
    through the sweep so a flag lost in a future train surfaces as untriaged."""
    with_secure = _classes_with_a_secure_prop()
    hits: set[tuple[str, str]] = set()
    for cls, wire, is_naming, is_configurable, _is_secure in _catalog_rows():
        if not is_configurable and not is_naming:
            continue
        if PROP_PATTERN.search(wire) and is_configurable:
            hits.add((cls, wire))
        if CLASS_PATTERN.search(cls) and (wire in VALUE_PROPS or is_naming):
            hits.add((cls, wire))
        if (
            cls in with_secure
            and is_configurable
            and _ASYM_VALUE.search(wire)
            and not _ASYM_EXCLUDE.search(wire)
        ):
            hits.add((cls, wire))
    return hits


def _dismissed_by_rule(cls: str, wire: str) -> bool:
    return any(
        cls_re.search(cls) and prop_re.search(wire) for cls_re, prop_re, _reason in DISMISS_RULES
    )


def _secure_positions() -> set[tuple[str, str]]:
    return {(cls, wire) for cls, wire, _n, _c, is_secure in _catalog_rows() if is_secure}


class TestWitnesses:
    def test_the_secure_flag_still_counts(self) -> None:
        """The mechanical layer: schema-flagged secrets are secrets."""
        assert is_secret_prop("snmpUserP", "authKey")
        assert is_secret_prop("datetimeNtpAuthKey", "key")
        assert is_secret_prop("vnsCCredSecret", "value")

    def test_the_curated_positions_the_flag_misses(self) -> None:
        assert is_secret_prop("vnsCCred", "value")
        assert is_secret_prop("pimIfPol", "authKey")
        assert is_secret_prop("pkiCertReq", "pwd")
        assert is_secret_prop("licenseLicPolicy", "regTokenId")

    def test_ordinary_config_is_not_secret(self) -> None:
        assert not is_secret_prop("fvBD", "arpFlood")
        assert not is_secret_prop("fvTenant", "name")

    def test_a_bgp_community_is_not_a_secret(self) -> None:
        """The 'community' pattern collision, decided: route tags stay."""
        assert not is_secret_prop("bgpExtComm", "community")
        assert not is_secret_prop("rtctrlSetComm", "community")

    def test_dn_carried_secrets_are_their_own_category(self) -> None:
        """The SNMP community string is the DN — value redaction cannot help."""
        assert secret_dn_classes() == {
            "snmpCommunityP",
            "aaaActiveUserSession",
            "aaaDeletedUserSession",
            "pkiSiteJwtPubKey",
        }
        # And precisely because it is naming, is_secret_prop does NOT claim it:
        # redacting the value of a naming prop would corrupt the DN silently.
        assert not is_secret_prop("snmpCommunityP", "name")


class TestCurationIntegrity:
    def test_every_curated_position_exists_in_the_catalogue(self) -> None:
        """No typos: a curated (class, prop) must name a real position."""
        real = {(cls, wire) for cls, wire, _n, _c, _s in _catalog_rows()}
        for position in SECRET_VALUE_POSITIONS | SECRET_DN_POSITIONS | set(REVIEWED_NOT_SECRET):
            assert position in real, f"curated position does not exist: {position}"

    def test_curated_and_dismissed_do_not_overlap(self) -> None:
        overlap = (SECRET_VALUE_POSITIONS | SECRET_DN_POSITIONS) & set(REVIEWED_NOT_SECRET)
        assert not overlap

    def test_curated_value_positions_are_not_already_secure(self) -> None:
        """A curated value position is exactly the flag's blind spot; if the
        schema flags it too, the curation is redundant and should be removed."""
        secure = _secure_positions()
        redundant = SECRET_VALUE_POSITIONS & secure
        assert not redundant, f"already secure-flagged, drop from curation: {redundant}"

    def test_dn_positions_are_naming_and_value_positions_are_not(self) -> None:
        naming = {(c, w) for c, w, is_naming, _cfg, _s in _catalog_rows() if is_naming}
        for position in SECRET_DN_POSITIONS:
            assert position in naming, f"{position} curated as DN-secret but not naming"
        for position in SECRET_VALUE_POSITIONS:
            assert position not in naming, f"{position} is naming — belongs in SECRET_DN_POSITIONS"

    def test_every_dismiss_rule_dismisses_something(self) -> None:
        """A rule matching nothing is a stale rule — curation must stay live."""
        hits = _sweep()
        for cls_re, prop_re, reason in DISMISS_RULES:
            matched = [(c, w) for c, w in hits if cls_re.search(c) and prop_re.search(w)]
            assert matched, f"dismiss rule matches nothing: {reason}"


def test_drift_guard_every_sweep_hit_is_triaged() -> None:
    """THE guard: no secret-shaped property ships untriaged.

    Recomputes all three sweep nets against the shipped catalogue; every hit
    must be secure-flagged (the mechanical layer), curated secret, or dismissed
    with a reason.  A new hit (future train, regen) fails here until a human
    decides.
    """
    secure = _secure_positions()
    untriaged = [
        (cls, wire)
        for cls, wire in sorted(_sweep())
        if (cls, wire) not in secure
        and (cls, wire) not in SECRET_VALUE_POSITIONS
        and (cls, wire) not in SECRET_DN_POSITIONS
        and (cls, wire) not in REVIEWED_NOT_SECRET
        and not _dismissed_by_rule(cls, wire)
    ]
    assert not untriaged, f"{len(untriaged)} untriaged secret candidates: {untriaged[:15]}"


def test_no_stale_explicit_dismissals() -> None:
    """The reverse direction: an explicit dismissal must still be a sweep hit.

    Otherwise renames in a future train leave dead entries that read as
    coverage while guarding nothing.
    """
    hits = _sweep()
    stale = [pos for pos in REVIEWED_NOT_SECRET if pos not in hits]
    assert not stale, f"dismissals no longer raised by the sweep: {stale}"


def test_the_review_found_leaks_are_now_caught() -> None:
    """Regression pins for the adversarial-review findings (2026-08-10)."""
    assert is_secret_prop("snmpTrapDest", "secName")  # blocking: trap community
    assert is_secret_prop("hsrpGroupPol", "key")
    assert is_secret_prop("bfdMhAuthP", "key")
    assert is_secret_prop("aaaUser", "otpkey")
    assert is_secret_prop("vmmInjectedClusterDetails", "userKey")
    assert is_secret_prop("firmwareSource", "authPass")
    assert "pkiSiteJwtPubKey" in secret_dn_classes()  # API key in the DN


def test_secure_flag_count_is_pinned() -> None:
    """169 flagged props in 6.0(9c) — a regen that moves this number is news."""
    assert len(_secrets._secure_flagged()) == 169


@pytest.mark.parametrize("position", sorted(SECRET_VALUE_POSITIONS))
def test_each_curated_value_position_is_configurable_non_secure(
    position: tuple[str, str],
) -> None:
    """Curated value positions are exactly the flag's blind spot.

    A position that gained the secure flag in a regen should leave the curated
    set (the mechanical layer covers it); one that stopped being configurable
    no longer reaches a snapshot through config at all.
    """
    rows = {(c, w): cfg for c, w, _n, cfg, _s in _catalog_rows()}
    assert position in rows, f"{position} does not exist — retriage"
    assert rows[position], f"{position} is no longer configurable — retriage"
