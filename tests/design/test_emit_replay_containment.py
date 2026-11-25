"""``to_code`` — a containment the public doors cannot prove is never emitted.

The reverse importer trusts the snapshot on containment: when none of the
SDK's own authorities (the generated model's ``_contains``, the navigation
``CHILD_MAP``, the catalogue's DN grammar) proves a parent → child edge, it
still attaches the child — the object exists on the fabric under exactly
that parent.  ``to_code`` then renders the child through a public door
(``parent.raw(...)``), and that door re-checks containment: the emitted
source raises at exec.

Contract pinned here: ``to_code`` checks every emitted edge against the same
authority the public doors use and reports the unprovable ones in its single
aggregated error, naming each DN — collect-all, nothing emitted.  A provable
edge, including the containment holes only the catalogue's DN grammar
proves, still emits and replays byte-equal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import pytest

from niwaki.design import Cursor, design, to_code, to_design
from niwaki.exceptions import DesignError
from niwaki.snapshot import Snapshot
from niwaki.types import JsonObject
from tests.design.test_import import canonical


def _replay(code: str, var: str = "cfg") -> Cursor:
    namespace: dict[str, object] = {}
    exec(code, namespace)
    replayed = namespace[var]
    assert isinstance(replayed, Cursor)
    return replayed


def _obj(aci_class: str, rn: str, attributes: JsonObject, *children: JsonObject) -> JsonObject:
    """One snapshot tree node, in ``Snapshot.tree`` shape."""
    return {
        "class": aci_class,
        "rn": rn,
        "attributes": attributes,
        "children": list(children),
    }


def _import(*children: JsonObject, on_unknown: Literal["raise", "raw"] = "raise") -> Cursor:
    """Import a ``polUni`` snapshot holding *children* — the trusted path."""
    tree = _obj("polUni", "uni", {}, *children)
    return to_design(Snapshot(scope="uni", tree=tree), on_unknown=on_unknown)


# ── Unprovable edges: refused, named, never emitted ───────────────────────────

# Each case: a snapshot whose single unprovable edge lands at ``dn``, and the
# public-door call the emitter would have to render for it.  The design
# imports (the snapshot is trusted) — the emitter is the last gate.
UNPROVABLE: dict[str, tuple[JsonObject, str, Callable[[], object]]] = {
    # A generated class attached through the trusted typed route.
    "typed-under-root": (
        _obj("fvBD", "BD-web", {"name": "web"}),
        "uni/BD-web",
        lambda: design().raw("fvBD", name="web"),
    ),
    "typed-under-a-domain": (
        _obj("fabricInst", "fabric", {}, _obj("fvBD", "BD-web", {"name": "web"})),
        "uni/fabric/BD-web",
        lambda: design().fabric().raw("fvBD", name="web"),
    ),
    # A catalogue-served class (no generated model) attached directly.
    "catalogue-under-root": (
        _obj("fvSvcBD", "svcBD-svc", {"name": "svc"}),
        "uni/svcBD-svc",
        lambda: design().raw("fvSvcBD", name="svc"),
    ),
    "catalogue-under-a-tenant": (
        _obj(
            "fvTenant",
            "tn-p",
            {"name": "p"},
            _obj("commTelnet", "telnet", {"adminSt": "disabled"}),
        ),
        "uni/tn-p/telnet",
        lambda: design().tenant("p").raw("commTelnet", adminSt="disabled"),
    ),
}


class TestUnprovableContainmentIsRefused:
    @pytest.mark.parametrize("case", sorted(UNPROVABLE))
    def test_to_code_refuses_and_names_the_dn(self, case: str) -> None:
        subtree, dn, _door = UNPROVABLE[case]
        cfg = _import(subtree)

        with pytest.raises(DesignError) as caught:
            to_code(cfg)

        assert dn in str(caught.value)

    @pytest.mark.parametrize("case", sorted(UNPROVABLE))
    def test_the_refused_source_would_indeed_crash(self, case: str) -> None:
        # Guard on the premise: the public door refuses this edge, so emitting
        # it could only ever produce source that raises at exec.
        subtree, _dn, door = UNPROVABLE[case]
        _import(subtree)  # the trusted import itself succeeds

        with pytest.raises(DesignError, match="not a valid APIC child"):
            door()

    def test_every_unprovable_edge_is_reported_at_once(self) -> None:
        cfg = _import(
            _obj("fvBD", "BD-web", {"name": "web"}),
            _obj("fvSvcBD", "svcBD-svc", {"name": "svc"}),
            _obj(
                "fvTenant",
                "tn-p",
                {"name": "p"},
                _obj("commTelnet", "telnet", {"adminSt": "disabled"}),
            ),
        )

        with pytest.raises(DesignError) as caught:
            to_code(cfg)

        message = str(caught.value)
        for dn in ("uni/BD-web", "uni/svcBD-svc", "uni/tn-p/telnet"):
            assert dn in message

    def test_provable_siblings_are_not_reported(self) -> None:
        cfg = _import(
            _obj(
                "fvTenant",
                "tn-p",
                {"name": "p"},
                _obj("fvBD", "BD-ok", {"name": "ok"}),
                _obj("commTelnet", "telnet", {"adminSt": "disabled"}),
            ),
        )

        with pytest.raises(DesignError) as caught:
            to_code(cfg)

        message = str(caught.value)
        assert "uni/tn-p/telnet" in message
        assert "uni/tn-p/BD-ok" not in message
        assert "uni/tn-p:" not in message  # the tenant itself is provable

    def test_joins_the_bypass_channel_error(self) -> None:
        # One aggregated refusal: an unknown class carried by on_unknown="raw"
        # and an unprovable edge are reported together.
        cfg = _import(
            _obj(
                "fvTenant",
                "tn-p",
                {"name": "p"},
                _obj("fvFutureThing", "future-x", {"name": "x"}),
            ),
            _obj("fvBD", "BD-web", {"name": "web"}),
            on_unknown="raw",
        )

        with pytest.raises(DesignError) as caught:
            to_code(cfg)

        message = str(caught.value)
        assert "uni/tn-p/future-x" in message
        assert "uni/BD-web" in message

    def test_to_code_on_a_view_refuses_too(self) -> None:
        cfg = _import(_obj("fvBD", "BD-web", {"name": "web"}))

        with pytest.raises(DesignError, match="uni/BD-web"):
            to_code(cfg.view())


# ── Provable edges: emitted and replayed byte-equal ───────────────────────────

PROVABLE: dict[str, JsonObject] = {
    # The normal case: every authority agrees.
    "curated": _obj("fvTenant", "tn-p", {"name": "p"}, _obj("fvBD", "BD-web", {"name": "web"})),
    # Catalogue-served child proven by CHILD_MAP / DN grammar under a tenant.
    "catalogue-under-tenant": _obj(
        "fvTenant", "tn-p", {"name": "p"}, _obj("fvSvcBD", "svcBD-svc", {"name": "svc"})
    ),
    # The containment holes only the catalogue's DN grammar proves (measured
    # live): absent from CHILD_MAP and from the generated _contains.
    "hole-commTelnet-under-commPol": _obj(
        "fabricInst",
        "fabric",
        {},
        _obj(
            "commPol",
            "comm-default",
            {"name": "default"},
            _obj("commTelnet", "telnet", {"adminSt": "disabled"}),
        ),
    ),
    "hole-uiSettingsCont-under-polUni": _obj("uiSettingsCont", "uisettingscont", {}),
    "hole-aaaAppUser-under-aaaUserEp": _obj(
        "aaaUserEp", "userext", {}, _obj("aaaAppUser", "appuser-svc", {"name": "svc"})
    ),
}


class TestProvableContainmentStillReplays:
    @pytest.mark.parametrize("case", sorted(PROVABLE))
    def test_emits_and_replays_byte_equal(self, case: str) -> None:
        cfg = _import(PROVABLE[case])

        replayed = _replay(to_code(cfg))

        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())
