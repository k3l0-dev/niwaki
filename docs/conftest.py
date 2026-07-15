"""Executable documentation — every ``python`` fence in ``docs/`` runs.

How it works:

- Sybil collects each fenced ``python`` block of every Markdown page as a
  pytest item.  Blocks within one document share a namespace, so a page
  reads as one continuous program (a client opened in the first snippet is
  still connected in the last one).
- Every block runs with the :func:`snippet_apic` fixture active: an httpx
  mock backed by :class:`FakeApic`, a tiny in-memory APIC.  POSTed design
  payloads are ingested into a DN-indexed store and reads answer from it,
  so describe → push → observe round-trips work exactly as printed, with
  zero visible scaffolding in the published snippet.
- ``<!--- skip: next --->`` immediately before a fence marks it
  non-executable (cobra comparisons, steps that need a live APIC).
  Shown-only output uses ``text`` / ``console`` fences.

Run the documentation suite alone with ``uv run pytest docs/``.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sybil import Sybil
from sybil.parsers.markdown import PythonCodeBlockParser, SkipParser

from niwaki.models.base import ManagedObject

_FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"


def _auth_payload(name: str) -> dict[str, Any]:
    """Load an authentication fixture (``auth_login`` / ``auth_refresh``).

    Args:
        name: Fixture filename without extension.

    Returns:
        Parsed APIC auth envelope.
    """
    data: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text())
    return data


class FakeApic:
    """In-memory APIC standing behind the documentation snippets.

    POST requests ingest the pushed envelope (any nesting depth) into a
    DN-indexed store; GET requests answer self / children / subtree / class
    queries from that store; DELETE removes a subtree.  Just enough APIC for
    documentation round-trips — no faults, no validation, no RBAC.

    Attributes:
        store: ``{dn: (aci_class_name, attributes)}`` for every object pushed
            so far.  Upserts merge attributes, like the real APIC.
    """

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, dict[str, str]]] = {}

    # ── Request entry point ───────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one intercepted httpx request from the store."""
        path = request.url.path
        if path == "/api/aaaLogin.json":
            return httpx.Response(200, json=_auth_payload("auth_login"))
        if path == "/api/aaaRefresh.json":
            return httpx.Response(200, json=_auth_payload("auth_refresh"))
        if request.method == "POST":
            return self._post(path, json.loads(request.content))
        if request.method == "DELETE":
            return self._delete(path)
        return self._get(path, dict(request.url.params))

    # ── Verbs ─────────────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        url_dn = _url_dn(path)
        for cls_name, body in payload.items():
            rn = _rn(cls_name, body.get("attributes", {}))
            parent = url_dn.removesuffix(rn).rstrip("/") if url_dn.endswith(rn) else url_dn
            self._ingest(cls_name, body, parent)
        return _envelope([])

    def _ingest(self, cls_name: str, body: dict[str, Any], parent_dn: str) -> None:
        attrs = {str(k): str(v) for k, v in body.get("attributes", {}).items()}
        attrs = _reorder_flags(cls_name, attrs)
        rn = _rn(cls_name, attrs)
        dn = f"{parent_dn}/{rn}" if parent_dn else rn
        _, existing = self.store.get(dn, (cls_name, {}))
        self.store[dn] = (cls_name, {**existing, **attrs})
        for child in body.get("children", []):
            for child_cls, child_body in child.items():
                self._ingest(child_cls, child_body, dn)

    def _delete(self, path: str) -> httpx.Response:
        dn = _url_dn(path)
        doomed = [d for d in self.store if d == dn or d.startswith(f"{dn}/")]
        for d in doomed:
            del self.store[d]
        return _envelope([])

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        if path.startswith("/api/class/"):
            cls_name = path.removeprefix("/api/class/").removesuffix(".json")
            items = [(d, c, a) for d, (c, a) in self.store.items() if c == cls_name]
        elif path.startswith("/api/mo/"):
            dn = _url_dn(path)
            target = params.get("query-target", "self")
            if target == "self":
                if dn in self.store and params.get("rsp-subtree") == "full":
                    return httpx.Response(
                        200, json={"totalCount": "1", "imdata": [self._nested(dn)]}
                    )
                items = [(dn, *self.store[dn])] if dn in self.store else []
            elif target == "children":
                items = [(d, c, a) for d, (c, a) in self.store.items() if self._child(dn, d)]
            else:  # subtree
                items = [
                    (d, c, a)
                    for d, (c, a) in self.store.items()
                    if d == dn or d.startswith(f"{dn}/")
                ]
        else:
            items = []
        if wanted := params.get("target-subtree-class"):
            allowed = set(wanted.split(","))
            items = [(d, c, a) for d, c, a in items if c in allowed]
        return _envelope(items)

    @staticmethod
    def _child(parent_dn: str, dn: str) -> bool:
        """True when *dn* is a **direct** child of *parent_dn*.

        Bracketed RN sections may contain ``/`` (``subnet-[10.0.1.1/24]``,
        path DNs) — they are masked before counting path segments.
        """
        if not dn.startswith(f"{parent_dn}/"):
            return False
        rest = re.sub(r"\[[^\]]*\]", "[]", dn[len(parent_dn) + 1 :])
        return "/" not in rest

    def _nested(self, dn: str) -> dict[str, Any]:
        """The stored object at *dn* with its full subtree nested (rsp-subtree=full)."""
        cls_name, attrs = self.store[dn]
        body: dict[str, Any] = {"attributes": {**attrs, "dn": dn}}
        if children := sorted(d for d in self.store if self._child(dn, d)):
            body["children"] = [self._nested(child) for child in children]
        return {cls_name: body}


def _url_dn(path: str) -> str:
    """Extract the DN from an ``/api/mo/<dn>.json`` request path."""
    return path.removeprefix("/api/mo/").removesuffix(".json")


def _reorder_flags(cls_name: str, attrs: dict[str, str]) -> dict[str, str]:
    """Store a bitmask in an order of the APIC's own choosing, as the real one does.

    A real APIC never echoes a bitmask back the way you wrote it: a subnet scope
    posted as ``"shared,public"`` is stored ``"public,shared"``.  A fake that
    parrots the payload would stay green on precisely the bug this class of type
    exists to kill — so it reverses every bitmask it is handed, which is the
    harshest honest thing it can do.

    Args:
        cls_name: ACI class of the object being stored.
        attrs: Its wire attributes.

    Returns:
        The attributes, with every bitmask value reordered.
    """
    from niwaki.models.base import REGISTRY

    _ensure_model(cls_name)
    cls = REGISTRY.get(cls_name)
    if cls is None:
        return attrs

    out = dict(attrs)
    for field_name, field in cls.model_fields.items():
        wire = field.serialization_alias or field_name
        value = out.get(wire)
        if not value or "," not in value:
            continue
        annotation = field.annotation
        while get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        if get_origin(annotation) in (frozenset, set):
            out[wire] = ",".join(reversed(value.split(",")))
    return out


@cache
def _ensure_model(cls_name: str) -> None:
    """Import the generated model module so REGISTRY can dispatch *cls_name*."""
    from niwaki.domain._child_map import CLASS_PKG

    if pkg := CLASS_PKG.get(cls_name):
        import_module(f"niwaki.models._generated.{pkg}.{cls_name}")


def _rn(cls_name: str, attrs: dict[str, Any]) -> str:
    """Compute the RN of an envelope node through the generated models."""
    _ensure_model(cls_name)
    mo = ManagedObject.from_apic({cls_name: {"attributes": dict(attrs)}})
    return mo.rn or cls_name


def _envelope(items: list[tuple[str, str, dict[str, str]]]) -> httpx.Response:
    """Wrap store items in an APIC list envelope (DNs materialised)."""
    imdata = [{cls: {"attributes": {**attrs, "dn": dn}}} for dn, cls, attrs in items]
    return httpx.Response(200, json={"totalCount": str(len(imdata)), "imdata": imdata})


# ── Pytest wiring ─────────────────────────────────────────────────────────────

_DOCUMENT_STORES: dict[str, FakeApic] = {}


@pytest.fixture
def snippet_apic(request: pytest.FixtureRequest, httpx_mock: HTTPXMock) -> FakeApic:
    """One stateful :class:`FakeApic` per documentation page.

    The store is keyed by document path, so every ``python`` fence of a page
    talks to the same fabric while the httpx interception stays scoped to
    the running block.

    Returns:
        The page's :class:`FakeApic` (exposed in the snippet namespace, but
        never referenced by published snippets).
    """
    fake = _DOCUMENT_STORES.setdefault(str(request.path), FakeApic())
    httpx_mock.add_callback(fake.handle, is_reusable=True, is_optional=True)
    return fake


SYBIL = Sybil(
    parsers=[PythonCodeBlockParser(), SkipParser()],
    patterns=["*.md", "**/*.md"],
    # reference/vocabulary is generated (tables, not tutorials): its fences are
    # for colouring, not execution — the generator is drift-guarded instead.
    excludes=["_build/**", "wiki/**", "reference/vocabulary/**"],
    fixtures=["snippet_apic"],
)

pytest_collect_file = SYBIL.pytest()
