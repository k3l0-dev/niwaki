"""Tests for the executable-documentation harness (``docs/conftest.py``).

The harness no longer carries its own in-memory APIC: it registers the
shipped :meth:`niwaki.testing.FakeApic.handle` as a pytest_httpx callback.
These tests pin

- that wiring (the docs conftest imports ``FakeApic`` from
  ``niwaki.testing`` and defines no fake of its own);
- the wire behaviour the documentation snippets lean on, at the level the
  harness uses it — ``handle(request)`` on a host the fake was not built
  with (the snippets say ``https://apic.example.com``);
- the guard that no ``python`` fence in the documentation can silently
  dodge execution.

The ``FakeApic`` tests import ``niwaki.testing`` lazily so that the fence
guards below keep running whatever the state of that package.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import textwrap
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from niwaki.testing import FakeApic

ROOT = Path(__file__).parent.parent.parent
DOCS = ROOT / "docs"
HOST = "https://apic.example.com"


def _load_docs_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_docs_conftest", DOCS / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


docs_conftest = _load_docs_conftest()


def _req(method: str, url: str, body: dict[str, Any] | None = None) -> httpx.Request:
    content = json.dumps(body).encode() if body is not None else None
    return httpx.Request(method, url, content=content)


def _imdata(response: httpx.Response) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(response.content)["imdata"]
    return data


_TRUE = frozenset({"true", "yes"})


def _dns(fake: FakeApic) -> set[str]:
    return {stored.dn for stored in fake.objects()}


TENANT_ENVELOPE: dict[str, Any] = {
    "polUni": {
        "attributes": {},
        "children": [
            {
                "fvTenant": {
                    "attributes": {"name": "prod"},
                    "children": [
                        {
                            "fvBD": {
                                "attributes": {"name": "web", "unicastRoute": "true"},
                                "children": [
                                    {"fvSubnet": {"attributes": {"ip": "10.0.1.1/24"}}},
                                    {"fvRsCtx": {"attributes": {"tnFvCtxName": "main"}}},
                                ],
                            }
                        },
                        {"fvCtx": {"attributes": {"name": "main"}}},
                    ],
                }
            }
        ],
    }
}


class TestDocsHarnessWiring:
    """The documentation runs on the shipped fake, not on a private copy."""

    def _tree(self) -> ast.Module:
        return ast.parse((DOCS / "conftest.py").read_text(encoding="utf-8"))

    def test_the_docs_conftest_defines_no_fake_of_its_own(self) -> None:
        classes = [node.name for node in ast.walk(self._tree()) if isinstance(node, ast.ClassDef)]
        assert "FakeApic" not in classes

    def test_the_docs_conftest_imports_the_shipped_fake(self) -> None:
        imported = {
            (node.module, alias.name)
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert ("niwaki.testing", "FakeApic") in imported

    def test_the_harness_fake_is_the_shipped_class(self) -> None:
        from niwaki.testing import FakeApic

        assert docs_conftest.FakeApic is FakeApic


class TestFakeApic:
    """The wire behaviour the documentation snippets rely on, through ``handle``."""

    def _fake(self) -> FakeApic:
        from niwaki.testing import FakeApic

        return FakeApic()

    def test_login_and_refresh_are_answered(self) -> None:
        fake = self._fake()
        for method, path, key in (
            ("POST", "aaaLogin", "aaaLogin"),
            ("GET", "aaaRefresh", "aaaRefresh"),
        ):
            resp = fake.handle(_req(method, f"{HOST}/api/{path}.json"))
            assert resp.status_code == 200
            (entry,) = _imdata(resp)
            assert "token" in entry[key]["attributes"]

    def test_strict_push_indexes_the_whole_subtree(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        assert _dns(fake) == {
            "uni",
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-main",
        }
        stored = fake.get("uni/tn-prod/BD-web")
        assert stored is not None
        assert stored.aci_class == "fvBD"
        assert stored.attributes["unicastRoute"] in _TRUE

    def test_staged_push_derives_the_parent_from_the_url(self) -> None:
        fake = self._fake()
        body = {"fvTenant": {"attributes": {"name": "prod"}}}
        fake.handle(_req("POST", f"{HOST}/api/mo/uni/tn-prod.json", body))
        assert fake.get("uni/tn-prod") is not None

    def test_get_self_returns_the_object_with_its_dn(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        resp = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-prod/BD-web.json"))
        (entry,) = _imdata(resp)
        assert entry["fvBD"]["attributes"]["dn"] == "uni/tn-prod/BD-web"

    def test_get_unknown_dn_is_an_empty_envelope(self) -> None:
        fake = self._fake()
        resp = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-ghost.json"))
        assert resp.status_code == 200
        assert _imdata(resp) == []
        assert json.loads(resp.content)["totalCount"] == "0"

    def test_children_and_subtree_scoping(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        children = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-prod.json?query-target=children"))
        assert {next(iter(e)) for e in _imdata(children)} == {"fvBD", "fvCtx"}
        subtree = fake.handle(
            _req(
                "GET",
                f"{HOST}/api/mo/uni/tn-prod.json"
                "?query-target=subtree&target-subtree-class=fvSubnet",
            )
        )
        (entry,) = _imdata(subtree)
        assert "fvSubnet" in entry

    def test_bracketed_rns_are_direct_children(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        children = fake.handle(
            _req("GET", f"{HOST}/api/mo/uni/tn-prod/BD-web.json?query-target=children")
        )
        assert {next(iter(e)) for e in _imdata(children)} == {"fvSubnet", "fvRsCtx"}

    def test_rsp_subtree_full_nests_the_children(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        resp = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-prod.json?rsp-subtree=full"))
        (entry,) = _imdata(resp)
        tenant_body = entry["fvTenant"]
        assert tenant_body["attributes"]["dn"] == "uni/tn-prod"
        bd = next(c for c in tenant_body["children"] if "fvBD" in c)
        grandchildren = {next(iter(c)) for c in bd["fvBD"]["children"]}
        assert grandchildren == {"fvSubnet", "fvRsCtx"}

    def test_class_query_returns_every_instance(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        resp = fake.handle(_req("GET", f"{HOST}/api/class/fvBD.json"))
        (entry,) = _imdata(resp)
        assert entry["fvBD"]["attributes"]["name"] == "web"

    def test_delete_removes_the_subtree(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        fake.handle(_req("DELETE", f"{HOST}/api/mo/uni/tn-prod/BD-web.json"))
        assert fake.get("uni/tn-prod/BD-web") is None
        assert fake.get("uni/tn-prod/BD-web/rsctx") is None
        assert fake.get("uni/tn-prod") is not None

    def test_repeated_pushes_merge_attributes(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        patch = {
            "fvBD": {"attributes": {"name": "web", "arpFlood": "true"}},
        }
        fake.handle(_req("POST", f"{HOST}/api/mo/uni/tn-prod/BD-web.json", patch))
        stored = fake.get("uni/tn-prod/BD-web")
        assert stored is not None
        # Earlier attributes survive a later upsert (a boolean may be stored in
        # the APIC's own spelling, so either truthy form is accepted).
        assert stored.attributes["unicastRoute"] in _TRUE
        assert stored.attributes["arpFlood"] in _TRUE

    def test_the_documented_enrichment_is_answered(self) -> None:
        """``observing.md`` chains ``with_health().with_faults()``: it must not fail."""
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        resp = fake.handle(
            _req("GET", f"{HOST}/api/class/fvBD.json?rsp-subtree-include=health,faults")
        )
        assert resp.status_code == 200
        assert len(_imdata(resp)) == 1


# ── Guard: no fence dodges execution ──────────────────────────────────────────

SKIP_NEXT = "skip: next"
SKIP_START = "skip: start"
SKIP_END = "skip: end"


def _python_fences(text: str) -> list[tuple[int, str, bool]]:
    """Yield ``(line, code, skipped)`` for every ```python fence of a page.

    Mirrors the Sybil conventions used in ``docs/conftest.py``: an HTML
    comment carrying ``skip: next`` exempts the following fence, and a
    ``skip: start`` / ``skip: end`` pair exempts a whole region.
    """
    fences: list[tuple[int, str, bool]] = []
    in_fence = False
    pending_skip = False
    skip_region = False
    start = 0
    code: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if in_fence:
            if stripped == "```":
                in_fence = False
                fences.append(
                    (start, textwrap.dedent("\n".join(code)), pending_skip or skip_region)
                )
                pending_skip = False
            else:
                code.append(line)
            continue
        if stripped.startswith("<!--") and "skip:" in stripped:
            if SKIP_NEXT in stripped:
                pending_skip = True
            elif SKIP_START in stripped:
                skip_region = True
            elif SKIP_END in stripped:
                skip_region = False
        elif stripped.startswith("```python"):
            in_fence = True
            start = lineno
            code = []
    return fences


def _documentation_pages() -> list[Path]:
    return [
        path
        for path in DOCS.rglob("*.md")
        if "_build" not in path.parts and "wiki" not in path.parts
    ]


def test_the_documentation_has_pages() -> None:
    assert len(_documentation_pages()) > 10


def test_every_python_fence_is_executable_or_explicitly_skipped() -> None:
    """Static net under the Sybil suite: no pseudo-code hides in a python fence.

    Real execution happens when pytest collects ``docs/`` (Sybil); this
    guard catches the block that should have been a ``text`` fence or an
    explicit skip, and does so even if the docs suite is run selectively.
    """
    offenders: list[str] = []
    for page in _documentation_pages():
        for lineno, code, skipped in _python_fences(page.read_text()):
            if skipped:
                continue
            try:
                ast.parse(code)
            except SyntaxError as exc:
                offenders.append(f"{page.relative_to(ROOT)}:{lineno} — {exc.msg}")
    assert not offenders, "non-executable python fences:\n" + "\n".join(offenders)


def test_the_sybil_collector_is_wired() -> None:
    assert callable(docs_conftest.pytest_collect_file)
    assert docs_conftest.SYBIL.fixtures == ("snippet_apic",) or list(
        docs_conftest.SYBIL.fixtures
    ) == ["snippet_apic"]
