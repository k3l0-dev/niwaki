"""The DN parser under ambiguity — every parse, never a silently wrong one.

An unbracketed naming value runs up to the literal that follows it in the RN
format.  When the value itself *contains* that literal (a leaf selector named
``a-typ-b``, a pod-filter key ``my-app``) the RN admits several structural
parses, and stopping at the first occurrence is a silent identity corruption.

The contract pinned here (ACI-agnostic, ``niwaki._dn`` only):

- ``naming_values`` enumerates **every** structural parse — an unbracketed
  value may stop at any occurrence of the next literal, a bracketed value stays
  matched by depth, the last unbracketed value runs to the end;
- an optional ``accept(prop, value)`` predicate filters the parses (it sees
  bracketed values too);
- 0 structural parses → ``ValueError`` with the historical messages; parses
  that ``accept`` all refuses → ``ValueError`` mentioning "no admissible";
  exactly one → the dict; two or more → :class:`AmbiguousRnError`, which lists
  the candidates in a deterministic order (by the values, in placeholder
  order) — at most 64 of them, ``truncated`` saying whether more exist, and a
  message that lists at most five.

The catalogue-wide guard at the bottom hardens the historical round trip of
``tests/test_dn.py``: its sentinels never contain a separator, so it could not
see the bug.  Here every unbracketed value followed by a literal receives a
sentinel that *contains* that literal — the parser must then answer exactly, or
raise :class:`AmbiguousRnError` with the exact parse among its candidates.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import niwaki
from niwaki._dn import AmbiguousRnError, DnParts, naming_values, parse

_SELECTOR_FORMAT = "leaves-{name}-typ-{type}"
_SELECTOR_RN = "leaves-a-typ-b-typ-range"
_SELECTOR_TYPES = frozenset({"ALL", "ALL_IN_POD", "range"})
_CAP = 64  # the most candidates an AmbiguousRnError ever carries


def _selector_type_is_an_enum_member(prop: str, value: str) -> bool:
    """``infraLeafS.type`` admissibility: an enum member; the name is free text."""
    return prop != "type" or value in _SELECTOR_TYPES


def _accept_all(prop: str, value: str) -> bool:
    del prop, value
    return True


def _refuse_all(prop: str, value: str) -> bool:
    del prop, value
    return False


class TestAmbiguousRnError:
    def test_is_a_value_error(self) -> None:
        # Every existing caller catches ValueError; ambiguity must stay inside
        # that net so nothing that used to be handled becomes a crash.
        assert issubclass(AmbiguousRnError, ValueError)

    def test_the_leaf_selector_case_is_ambiguous(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT)
        assert exc.value.rn == _SELECTOR_RN
        assert exc.value.rn_format == _SELECTOR_FORMAT
        assert exc.value.candidates == (
            {"name": "a", "type": "b-typ-range"},
            {"name": "a-typ-b", "type": "range"},
        )

    def test_candidates_is_a_tuple_of_plain_dicts(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT)
        assert isinstance(exc.value.candidates, tuple)
        assert all(type(candidate) is dict for candidate in exc.value.candidates)

    def test_the_message_names_the_ambiguity_and_the_rn(self) -> None:
        with pytest.raises(AmbiguousRnError, match="ambiguous") as exc:
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT)
        assert _SELECTOR_RN in str(exc.value)

    def test_the_pod_filter_key_case_is_ambiguous(self) -> None:
        # hostprotPodFilter: a Kubernetes label key routinely contains "-".
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values("podfilter-my-app-in", "podfilter-{key}-{operator}")
        assert exc.value.candidates == (
            {"key": "my", "operator": "app-in"},
            {"key": "my-app", "operator": "in"},
        )

    def test_three_candidates_come_in_placeholder_value_order(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values("x-1-2-3-4", "x-{a}-{b}-{c}")
        assert exc.value.candidates == (
            {"a": "1", "b": "2", "c": "3-4"},
            {"a": "1", "b": "2-3", "c": "4"},
            {"a": "1-2", "b": "3", "c": "4"},
        )

    def test_the_candidate_order_is_deterministic(self) -> None:
        def candidates() -> tuple[dict[str, str], ...]:
            with pytest.raises(AmbiguousRnError) as exc:
                naming_values("x-1-y-2-y-3-z-4", "x-{a}-y-{b}-z-{c}")
            return exc.value.candidates

        first = candidates()
        assert first == candidates()
        assert first == (
            {"a": "1", "b": "2-y-3", "c": "4"},
            {"a": "1-y-2", "b": "3", "c": "4"},
        )


class TestAccept:
    def test_an_enum_predicate_settles_the_leaf_selector(self) -> None:
        assert naming_values(
            _SELECTOR_RN, _SELECTOR_FORMAT, accept=_selector_type_is_an_enum_member
        ) == {"name": "a-typ-b", "type": "range"}

    def test_an_enum_predicate_settles_the_pod_filter(self) -> None:
        operators = {"defaultValue", "doesnotexists", "exists", "in", "notin"}

        def accept(prop: str, value: str) -> bool:
            return prop != "operator" or value in operators

        assert naming_values(
            "podfilter-my-app-in", "podfilter-{key}-{operator}", accept=accept
        ) == {
            "key": "my-app",
            "operator": "in",
        }

    def test_a_predicate_refusing_every_parse_is_no_admissible(self) -> None:
        with pytest.raises(ValueError, match="no admissible") as exc:
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT, accept=_refuse_all)
        assert not isinstance(exc.value, AmbiguousRnError)

    def test_a_predicate_refusing_the_only_parse_is_no_admissible(self) -> None:
        with pytest.raises(ValueError, match="no admissible") as exc:
            naming_values("BD-web", "BD-{name}", accept=_refuse_all)
        assert not isinstance(exc.value, AmbiguousRnError)

    def test_accept_sees_bracketed_values(self) -> None:
        seen: list[tuple[str, str]] = []

        def record(prop: str, value: str) -> bool:
            seen.append((prop, value))
            return True

        assert naming_values("subnet-[10.0.1.1/24]", "subnet-[{ip}]", accept=record) == {
            "ip": "10.0.1.1/24"
        }
        assert ("ip", "10.0.1.1/24") in seen

    def test_refusing_a_bracketed_value_is_no_admissible(self) -> None:
        def no_prefix(prop: str, value: str) -> bool:
            return not (prop == "ip" and "/" in value)

        with pytest.raises(ValueError, match="no admissible"):
            naming_values("subnet-[10.0.1.1/24]", "subnet-[{ip}]", accept=no_prefix)

    def test_accept_receives_only_this_formats_props_and_the_rns_substrings(self) -> None:
        seen: list[tuple[str, str]] = []

        def record(prop: str, value: str) -> bool:
            seen.append((prop, value))
            return _selector_type_is_an_enum_member(prop, value)

        naming_values(_SELECTOR_RN, _SELECTOR_FORMAT, accept=record)
        assert seen
        assert {prop for prop, _ in seen} <= {"name", "type"}
        assert all(value in _SELECTOR_RN for _, value in seen)

    def test_an_all_accepting_predicate_changes_nothing_when_unambiguous(self) -> None:
        rn = "iprule-[uni/tn-x]-dom-common"
        fmt = "iprule-[{objectDn}]-dom-{domain}"
        assert naming_values(rn, fmt, accept=_accept_all) == naming_values(rn, fmt)

    def test_an_all_accepting_predicate_leaves_the_ambiguity(self) -> None:
        with pytest.raises(AmbiguousRnError):
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT, accept=_accept_all)

    def test_residual_ambiguity_lists_only_the_admissible_parses(self) -> None:
        # Three structural parses; the integer predicate on ``eti`` drops the
        # middle one, two remain → still ambiguous, and only those two listed.
        fmt = "evpnmaciprt-{rd}-eti-{eti}-mac-{mac}-ip-[{pfx}]"
        rn = "evpnmaciprt-r-eti-1-mac-m-eti-2-mac-00:11:22:33:44:55-ip-[10.0.0.1/32]"

        def eti_is_an_integer(prop: str, value: str) -> bool:
            return prop != "eti" or value.isdigit()

        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(rn, fmt, accept=eti_is_an_integer)
        assert exc.value.candidates == (
            {"rd": "r", "eti": "1", "mac": "m-eti-2-mac-00:11:22:33:44:55", "pfx": "10.0.0.1/32"},
            {"rd": "r-eti-1-mac-m", "eti": "2", "mac": "00:11:22:33:44:55", "pfx": "10.0.0.1/32"},
        )


class TestBacktracking:
    """A unique parse need not be the first-occurrence parse."""

    def test_a_dead_end_bracket_backtracks_to_a_later_separator(self) -> None:
        # edmEncapDef: stopping srcCl at the first "-[" opens a bracket that
        # never closes; only the second occurrence yields a parse.
        assert naming_values("encap-x-[y-[z]", "encap-{srcCl}-[{epgPKey}]") == {
            "srcCl": "x-[y",
            "epgPKey": "z",
        }

    def test_a_dead_end_trailing_content_backtracks(self) -> None:
        # Stopping at the first "-[" closes the bracket early and leaves
        # trailing content; the second occurrence consumes the RN exactly.
        assert naming_values("encap-x-[y]-[z]", "encap-{srcCl}-[{epgPKey}]") == {
            "srcCl": "x-[y]",
            "epgPKey": "z",
        }

    def test_a_bracketed_value_next_to_an_ambiguous_bare_one(self) -> None:
        fmt = "t-{a}-k-{b}-v-[{c}]"
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values("t-1-k-2-k-3-v-[x/y-[z]]", fmt)
        assert exc.value.candidates == (
            {"a": "1", "b": "2-k-3", "c": "x/y-[z]"},
            {"a": "1-k-2", "b": "3", "c": "x/y-[z]"},
        )

    def test_several_separators_several_props_unique(self) -> None:
        assert naming_values("x-1-y-2-z-3", "x-{a}-y-{b}-z-{c}") == {
            "a": "1",
            "b": "2",
            "c": "3",
        }

    def test_the_last_bare_value_runs_to_the_end(self) -> None:
        # No literal follows the last value: a separator inside it is content,
        # never a split point — one parse, no ambiguity.
        assert naming_values("BD-a-b-c", "BD-{name}") == {"name": "a-b-c"}
        assert naming_values("type-remote-user-a-b", "type-{utype}-user-{username}") == {
            "utype": "remote",
            "username": "a-b",
        }

    def test_two_bare_values_one_separator_inside_the_second_is_ambiguous(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values("type-remote-user-al-user-ice", "type-{utype}-user-{username}")
        assert exc.value.candidates == (
            {"utype": "remote", "username": "al-user-ice"},
            {"utype": "remote-user-al", "username": "ice"},
        )

    def test_an_empty_value_stays_structurally_legal(self) -> None:
        assert naming_values("BD-", "BD-{name}") == {"name": ""}
        assert naming_values("x--", "x-{a}-{b}") == {"a": "", "b": ""}

    def test_empty_values_take_part_in_the_enumeration(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values("x---", "x-{a}-{b}")
        assert exc.value.candidates == ({"a": "", "b": "-"}, {"a": "-", "b": ""})

    def test_the_worst_real_format_with_an_ambiguous_epg_name(self) -> None:
        fmt = (
            "dropl2flow-spctag-{srcPcTag}-dpctag-{dstPcTag}-sepgname-{srcEpgName}"
            "-depgname-{dstEpgName}-smac-{srcMacAddr}-dmac-{dstMacAddr}"
            "-sintf-[{srcIntf}]-vlan-{vlan}"
        )
        rn = (
            "dropl2flow-spctag-1-dpctag-2-sepgname-a-depgname-b-depgname-c"
            "-smac-00:11:22:33:44:55-dmac-00:11:22:33:44:66-sintf-[eth1/1]-vlan-10"
        )
        common = {
            "srcPcTag": "1",
            "dstPcTag": "2",
            "srcMacAddr": "00:11:22:33:44:55",
            "dstMacAddr": "00:11:22:33:44:66",
            "srcIntf": "eth1/1",
            "vlan": "10",
        }
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(rn, fmt)
        assert [
            (candidate["srcEpgName"], candidate["dstEpgName"]) for candidate in exc.value.candidates
        ] == [("a", "b-depgname-c"), ("a-depgname-b", "c")]
        for candidate in exc.value.candidates:
            assert {k: v for k, v in candidate.items() if k in common} == common


class TestStructuralFailuresKeepTheirMessages:
    """0 structural parse → the historical messages, whatever ``accept`` says."""

    @pytest.mark.parametrize("accept", [None, _accept_all, _refuse_all])
    def test_a_mismatched_literal(self, accept: Callable[[str, str], bool] | None) -> None:
        with pytest.raises(ValueError, match="does not match"):
            naming_values("VRF-web", "BD-{name}", accept=accept)

    @pytest.mark.parametrize("accept", [None, _accept_all, _refuse_all])
    def test_an_unterminated_bracket(self, accept: Callable[[str, str], bool] | None) -> None:
        with pytest.raises(ValueError, match="unterminated bracket"):
            naming_values("subnet-[10.0.1.1/24", "subnet-[{ip}]", accept=accept)

    @pytest.mark.parametrize("accept", [None, _accept_all, _refuse_all])
    def test_a_missing_separator(self, accept: Callable[[str, str], bool] | None) -> None:
        with pytest.raises(ValueError, match="missing separator"):
            naming_values("type-remote", "type-{utype}-user-{username}", accept=accept)

    @pytest.mark.parametrize("accept", [None, _accept_all, _refuse_all])
    def test_trailing_content(self, accept: Callable[[str, str], bool] | None) -> None:
        with pytest.raises(ValueError, match="trailing content"):
            naming_values("rsctx-extra", "rsctx", accept=accept)

    def test_structural_failures_are_not_ambiguity(self) -> None:
        with pytest.raises(ValueError) as exc:
            naming_values("VRF-web", "BD-{name}")
        assert not isinstance(exc.value, AmbiguousRnError)
        assert "no admissible" not in str(exc.value)


class TestParseRelaysAccept:
    def test_parse_raises_the_ambiguity(self) -> None:
        with pytest.raises(AmbiguousRnError):
            parse(f"uni/infra/nprof-lp/{_SELECTOR_RN}", _SELECTOR_FORMAT)

    def test_parse_relays_accept(self) -> None:
        assert parse(
            f"uni/infra/nprof-lp/{_SELECTOR_RN}",
            _SELECTOR_FORMAT,
            accept=_selector_type_is_an_enum_member,
        ) == DnParts(
            parent="uni/infra/nprof-lp",
            rn=_SELECTOR_RN,
            naming={"name": "a-typ-b", "type": "range"},
        )

    def test_parse_relays_a_refusal(self) -> None:
        with pytest.raises(ValueError, match="no admissible"):
            parse("uni/tn-p/BD-web", "BD-{name}", accept=_refuse_all)

    def test_parse_without_accept_is_unchanged(self) -> None:
        assert parse("uni/tn-p/BD-web", "BD-{name}") == DnParts(
            parent="uni/tn-p", rn="BD-web", naming={"name": "web"}
        )


def _dashed_rn(parses: int) -> str:
    """An RN of ``x-{a}-{b}`` admitting exactly *parses* parses (one per dash)."""
    return "x-" + "-".join(["v"] * (parses + 1))


class TestCandidateCap:
    """Round 2: the enumeration stops at 64 candidates; the message stays small."""

    def test_a_small_ambiguity_is_not_truncated(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_SELECTOR_RN, _SELECTOR_FORMAT)
        assert exc.value.truncated is False

    def test_exactly_the_cap_is_complete(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_dashed_rn(_CAP), "x-{a}-{b}")
        assert len(exc.value.candidates) == _CAP
        assert exc.value.truncated is False

    def test_one_past_the_cap_is_truncated(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_dashed_rn(_CAP + 1), "x-{a}-{b}")
        assert len(exc.value.candidates) == _CAP
        assert exc.value.truncated is True

    def test_a_truncated_ambiguity_lists_real_parses_in_order(self) -> None:
        rn = _dashed_rn(500)

        def candidates() -> tuple[dict[str, str], ...]:
            with pytest.raises(AmbiguousRnError) as exc:
                naming_values(rn, "x-{a}-{b}")
            assert exc.value.truncated is True
            return exc.value.candidates

        first = candidates()
        assert first == candidates()  # deterministic
        assert len(first) == _CAP
        keyed = [(candidate["a"], candidate["b"]) for candidate in first]
        assert len(set(keyed)) == _CAP
        assert keyed == sorted(keyed)
        assert all(f"x-{a}-{b}" == rn for a, b in keyed)

    def test_an_explosive_rn_stays_cheap(self) -> None:
        # Six free values meeting at "-": C(29, 6) = 475,020 structural parses
        # (measured: 1.7 s and a 56 MB message uncapped) — the cap must stop
        # the enumeration, not only the listing.
        fmt = "s-{a}-{b}-{c}-{d}-{e}-{f}-{g}"
        rn = "s-" + "-".join(["v"] * 30)
        start = time.perf_counter()
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(rn, fmt)
        assert time.perf_counter() - start < 1.0
        assert exc.value.truncated is True
        assert len(exc.value.candidates) == _CAP
        assert len(str(exc.value)) < 4_000

    def test_the_message_lists_at_most_five_candidates_and_the_count(self) -> None:
        # Ten parses with long values: listing all of them would double the
        # message; at most five are shown, plus how many there are.
        word = "w" * 60
        rn = "x-" + "-".join([word] * 11)
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(rn, "x-{a}-{b}")
        candidates = exc.value.candidates
        assert len(candidates) == 10
        assert exc.value.truncated is False
        message = str(exc.value)
        assert "ambiguous" in message
        assert "10" in message
        longest = max(len(repr(candidate)) for candidate in candidates)
        assert len(message) <= 5 * longest + 2 * len(rn) + 300
        assert sum(repr(candidate) in message for candidate in candidates) <= 5

    def test_a_truncated_message_says_so_with_the_cap(self) -> None:
        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_dashed_rn(_CAP + 1), "x-{a}-{b}")
        assert str(_CAP) in str(exc.value)

    def test_an_accept_filtered_ambiguity_is_capped_too(self) -> None:
        def short_a(prop: str, value: str) -> bool:
            return prop != "a" or len(value) < 400

        with pytest.raises(AmbiguousRnError) as exc:
            naming_values(_dashed_rn(500), "x-{a}-{b}", accept=short_a)
        assert exc.value.truncated is True
        assert len(exc.value.candidates) == _CAP
        assert all(len(candidate["a"]) < 400 for candidate in exc.value.candidates)


class TestRepeatedPlaceholder:
    """Round 2: a placeholder used twice must carry the same value twice."""

    def test_equal_values_parse(self) -> None:
        assert naming_values("x-1-y-1", "x-{a}-y-{a}") == {"a": "1"}

    def test_different_values_are_no_parse(self) -> None:
        with pytest.raises(ValueError) as exc:
            naming_values("x-1-y-2", "x-{a}-y-{a}")
        assert not isinstance(exc.value, AmbiguousRnError)

    def test_never_a_key_error(self) -> None:
        # Every one of these has no parse: a ValueError, never a KeyError.
        for rn in ("x-1-y-2", "x-1-y-1-y-1", "x--y-z"):
            with pytest.raises(ValueError):
                naming_values(rn, "x-{a}-y-{a}")

    def test_equality_settles_an_otherwise_ambiguous_split(self) -> None:
        # "p-q-p-q": only the split at the middle dash repeats one value.
        assert naming_values("x-p-q-p-q", "x-{a}-{a}") == {"a": "p-q"}

    def test_a_repeated_placeholder_refused_by_accept(self) -> None:
        with pytest.raises(ValueError, match="no admissible"):
            naming_values("x-1-y-1", "x-{a}-y-{a}", accept=_refuse_all)


# ── The hardened catalogue-wide round trip ────────────────────────────────────

_TOKEN = re.compile(r"(\{\w+\})")


def _catalog_rn_formats() -> list[tuple[str, str]]:
    """Every (class, rn_format) the shipped catalogue carries a format for."""
    db = Path(niwaki.__file__).parent / "query" / "_catalog" / "catalog.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows: list[tuple[str, str]] = con.execute(
            "SELECT class_name, rn_format FROM mo WHERE rn_format != ''"
        ).fetchall()
        return rows
    finally:
        con.close()


def _tokens(rn_format: str) -> list[tuple[bool, str]]:
    """``(is_prop, text)`` tokens of an RN format, empty literals dropped."""
    out: list[tuple[bool, str]] = []
    for piece in _TOKEN.split(rn_format):
        if not piece:
            continue
        if piece.startswith("{") and piece.endswith("}"):
            out.append((True, piece[1:-1]))
        else:
            out.append((False, piece))
    return out


def _adversarial_positions(tokens: list[tuple[bool, str]]) -> list[tuple[str, str]]:
    """``(prop, next literal)`` for every unbracketed value followed by a literal."""
    positions: list[tuple[str, str]] = []
    for index, (is_prop, text) in enumerate(tokens):
        if not is_prop or index + 1 >= len(tokens):
            continue
        previous = tokens[index - 1] if index else (False, "")
        if not previous[0] and previous[1].endswith("["):
            continue  # bracketed — matched by depth, never ambiguous
        nxt_is_prop, nxt = tokens[index + 1]
        assert not nxt_is_prop, "the catalogue never puts two placeholders side by side"
        positions.append((text, nxt))
    return positions


def _fill(tokens: list[tuple[bool, str]], values: dict[str, str]) -> str:
    """Token-wise fill (a value containing ``{x}`` is never re-substituted)."""
    return "".join(values[text] if is_prop else text for is_prop, text in tokens)


def _verdict(
    rn: str,
    rn_format: str,
    expected: dict[str, str],
    props: list[str],
    tokens: list[tuple[bool, str]],
) -> str | None:
    """``None`` when the parser answered acceptably, else what went wrong.

    A complete ambiguity must list the exact parse.  A truncated one (more
    than :data:`_CAP` parses) lists exactly :data:`_CAP` of them and may stop
    before the exact one; every listed candidate must still be a real parse.
    """
    try:
        got = naming_values(rn, rn_format)
    except AmbiguousRnError as exc:
        candidates = list(exc.candidates)
        if len(candidates) < 2:
            return f"ambiguity with {len(candidates)} candidate(s)"
        if len(candidates) > _CAP:
            return f"{len(candidates)} candidates, above the cap of {_CAP}"
        if exc.truncated and len(candidates) != _CAP:
            return f"truncated with {len(candidates)} candidates instead of {_CAP}"
        if not exc.truncated and expected not in candidates:
            return f"the exact parse is not among the candidates {candidates}"
        for candidate in candidates:
            if _fill(tokens, candidate) != rn:
                return f"candidate {candidate} does not rebuild the RN"
        keyed = [tuple(candidate[prop] for prop in props) for candidate in candidates]
        if len(set(keyed)) != len(keyed):
            return f"duplicate candidates {candidates}"
        if keyed != sorted(keyed):
            return f"candidates not in placeholder-value order {candidates}"
        return None
    except ValueError as exc:
        return f"raised {exc}"
    if got != expected:
        return f"SILENTLY WRONG: got {got}, expected {expected}"
    return None


def test_adversarial_sentinels_never_parse_silently_wrong() -> None:
    """Each separator-bearing sentinel, one position at a time.

    For every catalogue format and every unbracketed value followed by a
    literal, that value's sentinel contains the literal (``val3x-typ-adv3x``);
    the other values keep plain sentinels.  The exact parse always exists
    structurally, so the only acceptable answers are the exact dict, or an
    :class:`AmbiguousRnError` listing it among ordered, distinct candidates.
    """
    rows = _catalog_rn_formats()
    assert len(rows) > 10_000, "the catalogue should carry thousands of RN formats"

    exercised = 0
    failures: list[tuple[str, str, str, str]] = []
    for class_name, rn_format in rows:
        tokens = _tokens(rn_format)
        props = [text for is_prop, text in tokens if is_prop]
        plain = {prop: f"val{i}x" for i, prop in enumerate(props)}
        for prop, literal in _adversarial_positions(tokens):
            exercised += 1
            expected = {**plain, prop: f"{plain[prop]}{literal}adv{props.index(prop)}x"}
            rn = _fill(tokens, expected)
            problem = _verdict(rn, rn_format, expected, props, tokens)
            if problem is not None:
                failures.append((class_name, rn_format, rn, problem))

    # Measured on the shipped catalogue (2026-10-27): 766 such positions.
    assert exercised > 700, f"only {exercised} adversarial positions exercised"
    assert not failures, f"{len(failures)} adversarial parses failed, e.g. {failures[:5]}"


def test_all_adversarial_sentinels_at_once_never_parse_silently_wrong() -> None:
    """Every separator-bearing sentinel in the same RN — the interplay case.

    Several ambiguous values at once multiply the structural parses (the worst
    shipped format, ``infraClSzEqObst``, reaches a few hundred — past the
    candidate cap); the verdict is the same: exact, or ambiguous with the
    exact parse among the candidates, or a truncated ambiguity of real parses.
    """
    failures: list[tuple[str, str, str, str]] = []
    for class_name, rn_format in _catalog_rn_formats():
        tokens = _tokens(rn_format)
        props = [text for is_prop, text in tokens if is_prop]
        expected = {prop: f"val{i}x" for i, prop in enumerate(props)}
        positions = _adversarial_positions(tokens)
        if not positions:
            continue
        for prop, literal in positions:
            expected[prop] = f"{expected[prop]}{literal}adv{props.index(prop)}x"
        rn = _fill(tokens, expected)
        problem = _verdict(rn, rn_format, expected, props, tokens)
        if problem is not None:
            failures.append((class_name, rn_format, rn, problem))

    assert not failures, f"{len(failures)} adversarial parses failed, e.g. {failures[:5]}"


def test_plain_sentinels_are_never_ambiguous() -> None:
    """The historical round trip, restated against the new error type.

    Sentinels without any separator character admit exactly one parse: an
    :class:`AmbiguousRnError` here would mean the enumeration invents parses.
    """
    failures: list[tuple[str, str, str]] = []
    for class_name, rn_format in _catalog_rn_formats():
        tokens = _tokens(rn_format)
        expected = {text: f"val{i}x" for i, (is_prop, text) in enumerate(tokens) if is_prop}
        try:
            got = naming_values(_fill(tokens, expected), rn_format)
        except ValueError as exc:
            failures.append((class_name, rn_format, f"raised {type(exc).__name__}: {exc}"))
            continue
        if got != expected:
            failures.append((class_name, rn_format, f"got {got}, expected {expected}"))
    assert not failures, f"{len(failures)} classes did not round-trip, e.g. {failures[:5]}"
