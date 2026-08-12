"""Unit tests for the niwaki-migrate codemod (tools/niwaki_migrate/migrate.py).

Every case uses the real committed migration table — the codemod's behavior
is meaningless against a synthetic one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.niwaki_migrate.migrate import migrate_source

TABLE = json.loads(Path("tools/niwaki_migrate/migration_2_0.json").read_text())


def _migrate(source: str) -> tuple[str, list[dict], list[dict]]:
    new, report = migrate_source(source, TABLE)
    return new, report.rewrites, report.flags


class TestKwargRenames:
    def test_maker_call_kwarg(self) -> None:
        src = 'ifp.path_attachment("topology/pod-1", if_inst_t="l3-port", addr="10.0.0.1/24")\n'
        new, rewrites, flags = _migrate(src)
        assert 'ip_address="10.0.0.1/24"' in new
        assert "addr=" not in new
        assert [r["kind"] for r in rewrites] == ["kwarg"] and not flags

    def test_chained_maker_kwarg(self) -> None:
        src = 'np.node_attachment("topology/pod-1/node-101", rtr_id="1.1.1.1")\n'
        new, _rewrites, _ = _migrate(src)
        assert 'router_id="1.1.1.1"' in new

    def test_model_constructor_kwarg(self) -> None:
        src = 'mo = l3extOut(name="edge", enforce_rtctrl="export")\n'
        new, _rewrites, _ = _migrate(src)
        assert 'enforce_route_control="export"' in new

    def test_unknown_call_never_rewritten_but_retired_name_flagged(self) -> None:
        # The tool never rewrites a call it cannot attribute — but a kwarg
        # bearing a retired 1.x name is always worth a human look, even when
        # the call turns out to be foreign: dismissing a flag costs one
        # glance, missing a forwarding helper costs a silent breakage.
        src = "requests.get(url, timeout_in_seconds=5)\n"
        new, rewrites, flags = _migrate(src)
        assert new == src
        assert not rewrites
        assert [f["kind"] for f in flags] == ["kwarg-unknown-call"]

    def test_unrenamed_kwarg_on_known_call_untouched(self) -> None:
        # vnsRtrCfg kept rtr_id — router_configuration must not be rewritten.
        src = 'tn.router_configuration("r", rtr_id="9.9.9.9")\n'
        new, rewrites, _ = _migrate(src)
        assert 'rtr_id="9.9.9.9"' in new
        assert not rewrites

    def test_formatting_preserved(self) -> None:
        src = (
            "pa = ifp.path_attachment(\n"
            '    "topology/pod-1",  # the border leaf\n'
            '    addr="10.0.0.1/24",\n'
            ")\n"
        )
        new, _, _ = _migrate(src)
        assert "# the border leaf" in new
        assert '    ip_address="10.0.0.1/24",\n' in new


class TestDictSplat:
    def test_variable_splat_into_known_call_flagged(self) -> None:
        # The exact shape that got past pyright AND mypy in our own migration:
        # keys built elsewhere, splatted opaquely. Never rewritten — always
        # surfaced for a human.
        src = (
            'kw: dict[str, object] = {"if_inst_t": inst, "addr": ip}\n'
            "ifp.path_attachment(dn, **kw)\n"
        )
        new, rewrites, flags = _migrate(src)
        assert new == src
        assert not rewrites
        assert [f["kind"] for f in flags] == ["opaque-splat"]

    def test_retired_name_as_kwarg_of_unknown_call_flagged(self) -> None:
        # A local helper forwarding **kwargs to a maker: the tool cannot
        # attribute the call, but a retired 1.x name is always debt.
        src = "_mk_l3out(t, name, enforce_rtctrl=enforce)\n"
        new, _rewrites, flags = _migrate(src)
        assert new == src
        assert [f["kind"] for f in flags] == ["kwarg-unknown-call"]

    def test_retired_name_as_subscript_key_flagged(self) -> None:
        src = 'kwargs["start_of_prefix_length"] = 17\n'
        new, _, flags = _migrate(src)
        assert new == src
        assert [f["kind"] for f in flags] == ["subscript"]

    def test_live_name_as_subscript_key_not_flagged(self) -> None:
        # "addr" is still a live 2.0 name (rtctrlSetNh, floating SVI) — a
        # blanket flag on it would drown the report in noise.
        src = 'kwargs["addr"] = ip\n'
        _, _, flags = _migrate(src)
        assert not flags

    def test_literal_dict_splat_rewritten(self) -> None:
        src = 'ifp.path_attachment(dn, **{"addr": ip, "mode": "regular"})\n'
        new, rewrites, _ = _migrate(src)
        assert '"ip_address": ip' in new
        assert [r["kind"] for r in rewrites] == ["dictkey"]


class TestAttributeRenames:
    def test_safe_attribute_rewritten(self) -> None:
        src = "print(epg.epg_with_multisite_mcast_source)\n"
        new, rewrites, _ = _migrate(src)
        assert "epg.has_mcast_source" in new
        assert [r["kind"] for r in rewrites] == ["attribute"]

    def test_ambiguous_attribute_flagged_not_rewritten(self) -> None:
        # rtr_id renamed on l3extRsNodeL3OutAtt, still live on vnsRtrCfg.
        src = "print(att.rtr_id)\n"
        new, rewrites, flags = _migrate(src)
        assert new == src
        assert not rewrites
        assert [f["kind"] for f in flags] == ["attribute-ambiguous"]

    def test_unrelated_attribute_untouched(self) -> None:
        src = "print(obj.some_random_attribute)\n"
        new, rewrites, flags = _migrate(src)
        assert new == src and not rewrites and not flags


class TestGetattrFlag:
    def test_getattr_literal_old_name_flagged(self) -> None:
        src = 'value = getattr(mo, "enforce_rtctrl", None)\n'
        new, _rewrites, flags = _migrate(src)
        assert new == src
        assert [f["kind"] for f in flags] == ["getattr"]


class TestRobustness:
    def test_empty_source(self) -> None:
        new, rewrites, flags = _migrate("")
        assert new == "" and not rewrites and not flags

    def test_syntax_error_raises(self) -> None:
        import libcst

        with pytest.raises(libcst.ParserSyntaxError):
            migrate_source("def broken(:\n", TABLE)

    def test_line_numbers_reported(self) -> None:
        src = "\n\n" + 'np.node_attachment(dn, rtr_id="1.1.1.1")\n'
        _, rewrites, _ = _migrate(src)
        assert rewrites[0]["line"] == 3


class TestReviewHardening:
    """Behaviors pinned after the adversarial review of the migration lot."""

    def test_generic_safe_names_are_not_rewritten(self) -> None:
        # managed_by (extMngdBy) is unique-and-dead but 2 tokens — a foreign
        # object plausibly carries it; never rewritten, never flagged.
        src = 'ticket.managed_by = "ops-team"\n'
        new, rewrites, flags = _migrate(src)
        assert new == src and not rewrites and not flags

    def test_distinctive_unrewritable_name_flagged(self) -> None:
        # in_band_ip_address: renamed in 2.0, not safely rewritable, 4 tokens
        # — the operational read a 1.x script actually does.
        src = "print(node.in_band_ip_address)\n"
        new, _, flags = _migrate(src)
        assert new == src
        assert [f["kind"] for f in flags] == ["attribute-flag"]

    def test_retired_key_in_literal_splat_of_wrong_maker_flagged(self) -> None:
        # A retired name that is NOT one of this maker's own renames must not
        # be silently swallowed by the splat rewrite path.
        src = 'ifp.path_attachment(dn, **{"collection_interval_in_seconds": 5})\n'
        new, rewrites, flags = _migrate(src)
        assert new == src and not rewrites
        assert [f["kind"] for f in flags] == ["dictkey-retired-other-class"]

    def test_literal_splat_rewrite_not_double_flagged(self) -> None:
        # The dict rewritten inside a known call must not ALSO get the
        # unattributed-dict flag from visit_Dict.
        src = 'ifp.path_attachment(dn, **{"addr": ip})\n'
        _, rewrites, flags = _migrate(src)
        assert [r["kind"] for r in rewrites] == ["dictkey"]
        assert not flags

    def test_dictkey_rewrite_reports_the_call_line(self) -> None:
        src = "\n" + 'ifp.path_attachment(dn, **{"addr": ip})\n'
        _, rewrites, _ = _migrate(src)
        assert rewrites[0]["line"] == 2

    def test_getattr_flag_covers_catalog_surface(self) -> None:
        src = 'v = getattr(node, "in_band_ip_address", None)\n'
        _, _, flags = _migrate(src)
        assert [f["kind"] for f in flags] == ["getattr"]


class TestConflictPaths:
    """The _CONFLICT branches, exercised with a synthetic table (the real
    table has zero conflicting maker unions — measured — so the mechanism
    is untestable against it)."""

    @staticmethod
    def _conflict_table() -> dict:
        return {
            "surfaces": {
                "model_fields": {
                    "xA": {"w": {"old": "shared_old_name_here", "new": "new_a"}},
                    "xB": {"w": {"old": "shared_old_name_here", "new": "new_b"}},
                },
                "catalog_readable": {},
                "navigation": {},
                "makers": {},
            },
            "maker_context": {"conflicted_maker": ["xA", "xB"]},
            "attribute_safe": {},
            "attribute_flag": [],
            "retired_kwarg_names": [],
        }

    def test_kwarg_conflict_flagged_not_rewritten(self) -> None:
        new, report = migrate_source(
            "c.conflicted_maker(shared_old_name_here=1)\n", self._conflict_table()
        )
        assert "shared_old_name_here=1" in new
        assert [f["kind"] for f in report.flags] == ["kwarg-conflict"]

    def test_dictkey_conflict_flagged_not_rewritten(self) -> None:
        new, report = migrate_source(
            'c.conflicted_maker(**{"shared_old_name_here": 1})\n', self._conflict_table()
        )
        assert '"shared_old_name_here"' in new
        assert [f["kind"] for f in report.flags] == ["dictkey-conflict"]


class TestCli:
    def test_dry_run_write_report_and_exclusions(self, tmp_path: Path) -> None:
        import json as json_mod
        import subprocess
        import sys

        target = tmp_path / "proj"
        (target / ".venv" / "lib").mkdir(parents=True)
        (target / ".venv" / "lib" / "vendored.py").write_text(
            'x.node_attachment(dn, rtr_id="1.1.1.1")\n'
        )
        code = target / "app.py"
        code.write_text('np.node_attachment(dn, rtr_id="1.1.1.1")\n')
        broken = target / "broken.py"
        broken.write_text("def broken(:\n")
        report = tmp_path / "report.json"

        def run(*extra: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "tools.niwaki_migrate.migrate", str(target), *extra],
                capture_output=True,
                text=True,
                cwd=Path.cwd(),
            )

        dry = run("--dry-run", "--report", str(report))
        assert dry.returncode == 0
        assert code.read_text() == 'np.node_attachment(dn, rtr_id="1.1.1.1")\n'  # untouched
        assert "SKIP (unparsable)" in dry.stderr
        payload = json_mod.loads(report.read_text())
        assert payload["rewrites"] == 1  # app.py only — .venv excluded
        assert payload["files"][0]["path"].endswith("app.py")

        wet = run()
        assert wet.returncode == 0
        assert 'router_id="1.1.1.1"' in code.read_text()
        # vendored file under .venv never touched
        assert "rtr_id" in (target / ".venv" / "lib" / "vendored.py").read_text()
