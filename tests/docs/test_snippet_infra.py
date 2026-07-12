"""Tests for the executable-documentation harness (``docs/conftest.py``).

They pin the harness behaviour: the FakeApic verbs and DN bookkeeping,
plus the guard that no ``python`` fence in the documentation can silently
dodge execution.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

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


class TestFakeApic:
    """The in-memory APIC behind the documentation snippets."""

    def _fake(self) -> Any:
        return docs_conftest.FakeApic()

    def test_login_and_refresh_answer_from_fixtures(self) -> None:
        fake = self._fake()
        for path, key in (("aaaLogin", "aaaLogin"), ("aaaRefresh", "aaaRefresh")):
            resp = fake.handle(_req("POST", f"{HOST}/api/{path}.json"))
            assert resp.status_code == 200
            (entry,) = _imdata(resp)
            assert "token" in entry[key]["attributes"]

    def test_strict_push_indexes_the_whole_subtree(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        assert set(fake.store) == {
            "uni",
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-main",
        }
        cls_name, attrs = fake.store["uni/tn-prod/BD-web"]
        assert cls_name == "fvBD"
        assert attrs["unicastRoute"] == "true"

    def test_staged_push_derives_the_parent_from_the_url(self) -> None:
        fake = self._fake()
        body = {"fvTenant": {"attributes": {"name": "prod"}}}
        fake.handle(_req("POST", f"{HOST}/api/mo/uni/tn-prod.json", body))
        assert "uni/tn-prod" in fake.store

    def test_get_self_returns_the_object_with_its_dn(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        resp = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-prod/BD-web.json"))
        (entry,) = _imdata(resp)
        assert entry["fvBD"]["attributes"]["dn"] == "uni/tn-prod/BD-web"

    def test_get_unknown_dn_is_an_empty_envelope(self) -> None:
        fake = self._fake()
        resp = fake.handle(_req("GET", f"{HOST}/api/mo/uni/tn-ghost.json"))
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
        assert "uni/tn-prod/BD-web" not in fake.store
        assert "uni/tn-prod/BD-web/rsctx" not in fake.store
        assert "uni/tn-prod" in fake.store

    def test_repeated_pushes_merge_attributes(self) -> None:
        fake = self._fake()
        fake.handle(_req("POST", f"{HOST}/api/mo/uni.json", TENANT_ENVELOPE))
        patch = {
            "fvBD": {"attributes": {"name": "web", "arpFlood": "true"}},
        }
        fake.handle(_req("POST", f"{HOST}/api/mo/uni/tn-prod/BD-web.json", patch))
        _, attrs = fake.store["uni/tn-prod/BD-web"]
        assert attrs["unicastRoute"] == "true"
        assert attrs["arpFlood"] == "true"


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
