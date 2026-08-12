"""``to_code`` — the code emitter and its replay contract (2.0 it.4 lot A)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from niwaki.design import Cursor, design, ref, tenant, to_code, to_design
from niwaki.exceptions import DesignError
from niwaki.snapshot import Snapshot
from tests.design.test_import import canonical, pseudo_snapshot
from tests.design.test_import_gate_a import GOLDENS, three_acts


def _replay(code: str, var: str = "cfg") -> Cursor:
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    return namespace[var]


class TestReplayContract:
    """Executing the emitted source reproduces the design, byte-for-byte."""

    @pytest.mark.parametrize("builder", GOLDENS, ids=lambda fn: fn.__name__)
    def test_golden_worlds_replay_byte_equal(self, builder: Callable[[], Cursor]) -> None:
        cfg = builder()
        replayed = _replay(to_code(cfg))
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    @pytest.mark.parametrize("builder", GOLDENS, ids=lambda fn: fn.__name__)
    def test_the_full_brownfield_chain_replays(self, builder: Callable[[], Cursor]) -> None:
        # design → payload → snapshot → to_design → to_code → exec → payload:
        # the whole 2.0 story in one assertion.
        cfg = builder()
        imported = to_design(pseudo_snapshot(cfg.to_payload()))
        replayed = _replay(to_code(imported))
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    def test_composed_designs_replay(self) -> None:
        from niwaki.design import merge

        cfg = three_acts()
        staged = cfg.slice("uni/tn-niwaki-prod")
        replayed = _replay(to_code(staged))
        assert canonical(replayed.to_payload()) == canonical(staged.to_payload())
        combined = merge(tenant("a"), tenant("b"))
        assert canonical(_replay(to_code(combined)).to_payload()) == canonical(
            combined.to_payload()
        )

    def test_escape_hatches_replay(self) -> None:
        cfg = design()
        t = cfg.tenant("prod")
        t.bd("web").raw_set(arpFlood="yes")
        cfg.controller().raw("infraKafkaPol", name="kafkapol", mode="OFF")
        replayed = _replay(to_code(cfg))
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    def test_typed_values_render_replayable(self) -> None:
        # Enum members and Flags sets repr as non-importable spellings — the
        # emitter renders them through the wire boundary instead.
        from niwaki.models._generated.enums.L4TcpFlags import L4TcpFlags

        cfg = tenant("prod")
        cfg.filter("f").entry("e", tcp_rules={L4TcpFlags("syn"), L4TcpFlags("ack")})
        code = to_code(cfg)
        assert "L4TcpFlags" not in code
        replayed = _replay(code)
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())


class TestEmittedShape:
    def test_variables_are_named_after_the_objects(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web").subnet("10.0.1.1/24")
        code = to_code(cfg)
        assert "tenant_prod = cfg.tenant('prod')" in code
        assert "bd_web = tenant_prod.bd('web')" in code

    def test_leaves_chain_their_references(self) -> None:
        cfg = tenant("prod")
        cfg.app("a").epg("e").bind(bd="web").provide("c")
        code = to_code(cfg)
        assert ".epg('e').bind(bd='web').provide('c')" in code

    def test_ref_attrs_render_as_ref_calls(self) -> None:
        cfg = tenant("prod")
        cfg.filter("f").entry("e")
        cfg.contract("c").subject("s").bind(filter=ref("f", directives="log"))
        code = to_code(cfg)
        assert "ref('f', directives='log')" in code
        assert "from niwaki.design import design, ref" in code

    def test_no_ref_import_when_unused(self) -> None:
        cfg = tenant("prod")
        assert "ref" not in to_code(cfg).splitlines()[0].split("import ")[1]

    def test_custom_root_variable(self) -> None:
        cfg = tenant("prod")
        code = to_code(cfg, var="fabric_cfg")
        assert code.count("fabric_cfg = design()") == 1
        assert canonical(_replay(code, "fabric_cfg").to_payload()) == canonical(cfg.to_payload())

    def test_invalid_variable_name_is_refused(self) -> None:
        with pytest.raises(DesignError, match="variable"):
            to_code(tenant("prod"), var="not a name")

    def test_colliding_names_get_suffixes(self) -> None:
        cfg = design()
        cfg.tenant("x").bd("same")
        cfg.tenant("y").bd("same")
        code = to_code(cfg)
        # both BDs carry children? none — they inline; force vars via subnets
        cfg2 = design()
        cfg2.tenant("x").bd("same").subnet("10.0.0.1/24")
        cfg2.tenant("y").bd("same").subnet("10.0.1.1/24")
        code = to_code(cfg2)
        assert "bd_same = " in code
        assert "bd_same_2 = " in code
        replayed = _replay(code)
        assert canonical(replayed.to_payload()) == canonical(cfg2.to_payload())

    def test_view_input_is_accepted(self) -> None:
        cfg = tenant("prod")
        assert to_code(cfg.view()) == to_code(cfg)


class TestReconciliationPure:
    """The pure half of reconcile() — one uni capture vs declared DNs."""

    def _snap(self, tenant_children: list[dict]) -> Snapshot:
        from niwaki.snapshot import Snapshot

        return Snapshot(
            scope="uni",
            tree={
                "class": "polUni",
                "rn": "uni",
                "attributes": {},
                "children": [
                    {
                        "class": "fvTenant",
                        "rn": "tn-prod",
                        "attributes": {"name": "prod"},
                        "children": tenant_children,
                    }
                ],
            },
        )

    def test_extra_implicit_and_orphan_roots(self) -> None:
        from niwaki.design._reconcile import _reconcile_against

        snap = self._snap(
            [
                {
                    "class": "fvBD",
                    "rn": "BD-web",
                    "attributes": {},
                    "children": [
                        # fabric-materialised default relation (non-creatable)
                        {
                            "class": "fvRsBdToEpRet",
                            "rn": "rsbdToEpRet",
                            "attributes": {},
                            "children": [],
                        },
                        # operator-created foreigner
                        {
                            "class": "fvSubnet",
                            "rn": "subnet-[10.0.9.1/24]",
                            "attributes": {},
                            "children": [],
                        },
                    ],
                },
                {"class": "fvCtx", "rn": "ctx-foreign", "attributes": {}, "children": []},
            ]
        )
        declared = {"uni", "uni/tn-prod", "uni/tn-prod/BD-web"}
        report = _reconcile_against(declared, {"uni/tn-prod"}, snap)
        assert ("uni/tn-prod/ctx-foreign", "fvCtx") in report.extra
        assert ("uni/tn-prod/BD-web/subnet-[10.0.9.1/24]", "fvSubnet") in report.extra
        # the default Rs is fabric-materialised — implicit, never extra
        assert ("uni/tn-prod/BD-web/rsbdToEpRet", "fvRsBdToEpRet") in report.implicit
        assert not report.clean
        assert sorted(report.orphan_subtrees) == [
            "uni/tn-prod/BD-web/subnet-[10.0.9.1/24]",
            "uni/tn-prod/ctx-foreign",
        ]

    def test_deep_foreign_subtree_has_one_root(self) -> None:
        from niwaki.design._reconcile import _reconcile_against

        snap = self._snap(
            [
                {
                    "class": "fvCtx",
                    "rn": "ctx-foreign",
                    "attributes": {},
                    "children": [
                        {"class": "fvRtCtx", "rn": "rtctx", "attributes": {}, "children": []}
                    ],
                }
            ]
        )
        declared = {"uni", "uni/tn-prod"}
        report = _reconcile_against(declared, {"uni/tn-prod"}, snap)
        assert report.orphan_subtrees == ["uni/tn-prod/ctx-foreign"]

    def test_undeclared_domains_are_nobodys_drift(self) -> None:
        from niwaki.design._reconcile import _reconcile_against

        snap = self._snap([{"class": "fvCtx", "rn": "ctx-x", "attributes": {}, "children": []}])
        report = _reconcile_against({"uni"}, set(), snap)  # no domain declared
        assert report.clean and report.extra == []

    def test_clean_when_everything_is_declared(self) -> None:
        from niwaki.design._reconcile import Reconciliation, _reconcile_against

        snap = self._snap([{"class": "fvCtx", "rn": "ctx-m", "attributes": {}, "children": []}])
        declared = {"uni", "uni/tn-prod", "uni/tn-prod/ctx-m"}
        report = _reconcile_against(declared, {"uni/tn-prod"}, snap)
        assert report.clean and report.implicit == []
        assert Reconciliation(extra=[], implicit=[], orphan_subtrees=[]).clean


class TestReviewScenariosIt4:
    """The 2026-08-11 adversarial pass on it.4 — every finding pinned."""

    def test_containment_hole_replays_through_the_public_door(self) -> None:
        # B1: commTelnet under commPol — absent from CHILD_MAP/_contains,
        # proven by the catalogue's own DN grammar; the emitted raw() call
        # must execute.
        from niwaki.snapshot import Snapshot

        tree = {
            "class": "polUni",
            "rn": "uni",
            "attributes": {},
            "children": [
                {
                    "class": "fabricInst",
                    "rn": "fabric",
                    "attributes": {},
                    "children": [
                        {
                            "class": "commPol",
                            "rn": "comm-default",
                            "attributes": {"name": "default"},
                            "children": [
                                {
                                    "class": "commTelnet",
                                    "rn": "telnet",
                                    "attributes": {"adminSt": "disabled"},
                                    "children": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        cfg = to_design(Snapshot(scope="uni", tree=tree))
        replayed = _replay(to_code(cfg))
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    def test_single_enum_member_renders_importable(self) -> None:
        # M1: a StrEnum IS a str — repr() of a member is not source code.
        from niwaki.models._generated.enums.FvnsAllocMode import FvnsAllocMode

        cfg = design()
        cfg.infra().vlan_pool("p", FvnsAllocMode("static"))
        code = to_code(cfg)
        assert "FvnsAllocMode" not in code and "<" not in code
        replayed = _replay(code)
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    def test_bypass_channel_designs_are_refused_loudly(self) -> None:
        # M2: on_unknown="raw" imports carry items the public doors cannot
        # replay — to_code must refuse, never emit crashing source.
        from niwaki.snapshot import Snapshot

        tree = {
            "class": "polUni",
            "rn": "uni",
            "attributes": {},
            "children": [
                {
                    "class": "fvTenant",
                    "rn": "tn-p",
                    "attributes": {"name": "p"},
                    "children": [
                        {
                            "class": "fvFutureThing",
                            "rn": "future-x",
                            "attributes": {"name": "x"},
                            "children": [],
                        }
                    ],
                }
            ],
        }
        cfg = to_design(Snapshot(scope="uni", tree=tree), on_unknown="raw")
        with pytest.raises(DesignError, match="bypass"):
            to_code(cfg)

    def test_dynamic_dispatch_naming_order_is_normalised(self) -> None:
        # M3: kwargs order from a dynamic-dispatch maker call must re-emit
        # in the class's own naming order, or identity silently permutes.
        from niwaki.design._cursor import _load_class

        cls = _load_class("vnsLDevCtx")
        props = list(cls._naming_props)
        assert len(props) >= 2  # the permutation needs at least two
        cfg = tenant("prod")
        values = {p: f"v-{i}" for i, p in enumerate(props)}
        shuffled = dict(reversed(list(values.items())))
        cfg.logical_device_context(**shuffled)
        replayed = _replay(to_code(cfg))
        assert canonical(replayed.to_payload()) == canonical(cfg.to_payload())

    def test_var_shadowing_the_imports_is_refused(self) -> None:
        # m2: ref = design() would break every later ref(...) call.
        for name in ("ref", "design"):
            with pytest.raises(DesignError, match="shadow"):
                to_code(tenant("prod"), var=name)
