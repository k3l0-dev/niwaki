"""DSL reference generator — drift guard and content contract.

The pages under ``docs/reference/vocabulary/`` are committed generated
artifacts (same rule as ``_generated_cursors``): regeneration must match the
working tree, and the content must cover the live vocabulary tables — every
position, every maker, every **keyword argument**, every enum.
"""

from __future__ import annotations

from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

import pytest

from niwaki._codegen._field_docs import enum_anchor, field_docs, position_anchor
from niwaki._codegen.generate_design import _positions
from niwaki._codegen.generate_docs import (
    _NOT_CURATED,
    OUTPUT_DIR,
    _resolve_edge,
    _uni_keys,
    render_all,
)
from niwaki.design._cursor import _load_class, _tables

_PAGES = sorted(render_all())


def _is_numeric(annotation: Any) -> bool:
    """True when a model field's annotation is a number — read independently.

    Deliberately *not* routed through the doc extractor's own type logic: the
    guard's job is to catch the extractor rendering a number as ``str``, so it
    must decide numericity on its own.  Covers a plain ``int``/``float`` and a
    named number (``int | Literal[...]`` — a port stored under a name), each
    possibly wrapped in ``Annotated``.
    """
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if annotation in (int, float):
        return True
    if get_origin(annotation) in (Union, UnionType):
        for arg in get_args(annotation):
            while get_origin(arg) is Annotated:
                arg = get_args(arg)[0]
            if arg in (int, float):
                return True
    return False


# The uni-level domains (phys_dom, l3_dom, l2_dom) live under uni/, everything
# else under its root domain — mirrors the generator's layout.
_UNI_KEYS = frozenset(_uni_keys())


def _page_path(key: str) -> str:
    folder = "uni" if key in _UNI_KEYS else key.split(".")[0]
    return f"{folder}/{key.replace('.', '-')}.md"


class TestDrift:
    @pytest.mark.parametrize("filename", _PAGES)
    def test_committed_page_matches_regeneration(self, filename: str) -> None:
        """A stale committed page means someone edited the vocabulary without
        running ``uv run python -m niwaki._codegen.generate_docs``."""
        on_disk = Path(OUTPUT_DIR / filename)
        assert on_disk.exists(), f"{filename} missing — run generate_docs"
        assert on_disk.read_text(encoding="utf-8") == render_all()[filename], (
            f"{filename} is stale — re-run generate_docs"
        )

    def test_no_orphan_pages(self) -> None:
        """Every committed page — including the per-position ones — is produced."""
        committed = {str(path.relative_to(OUTPUT_DIR)) for path in OUTPUT_DIR.rglob("*.md")}
        assert committed == set(_PAGES)


class TestCoverageOfTheDSL:
    """The contract: nothing the DSL accepts is missing from the reference."""

    def test_every_position_has_a_page(self) -> None:
        pages = render_all()
        for key in _positions():
            if not key:  # the polUni root is the index, not a position page
                continue
            assert _page_path(key) in pages, f"position {key} has no page"

    @pytest.mark.parametrize("key", [k for k in _positions() if k])
    def test_every_keyword_argument_is_documented(self, key: str) -> None:
        """Every kwarg a maker accepts appears in its position's table.

        This is the central promise of the reference: the fields of `.bd()`
        (and of every other maker) are findable without an IDE.
        """
        pos = _positions()[key]
        cls = _load_class(pos.aci_class)
        page = render_all()[_page_path(key)]
        for doc in field_docs(cls, _tables().sugar.get(pos.aci_class, {})):
            assert f"| `{doc.name}`" in page, f"{key}: {doc.name} missing from the table"

    def test_not_curated_list_names_only_uncurated_classes(self) -> None:
        """The "Not curated yet" list must never claim a curated area as raw ACI.

        It is hand-written, so it drifts as the vocabulary grows: ESGs, vzAny and
        L3Out internals were listed as "still speak raw ACI" long after they got
        makers.  Every class the list names is checked against the real curated
        positions — a maker for any of them fails this test.
        """
        curated = {pos.aci_class for key, pos in _positions().items() if key}
        wrongly_listed = [
            f"{title} ({cls})"
            for title, _, classes in _NOT_CURATED
            for cls in classes
            if cls in curated
        ]
        assert not wrongly_listed, (
            f"'Not curated yet' names classes that ARE curated: {wrongly_listed} — "
            "remove them from _NOT_CURATED in generate_docs."
        )

    def test_every_curated_maker_is_documented(self) -> None:
        book = "".join(render_all().values())
        for table in _tables().makers.values():
            for label, child in table.items():
                assert f".{label}(" in book, f"maker {label} missing"
                assert f"`{child}`" in book, f"class {child} missing"

    def test_every_bind_alias_is_documented_with_flavor(self) -> None:
        book = "".join(render_all().values())
        for owner, table in _tables().binds.items():
            for alias, target in table.items():
                edge = _resolve_edge(owner, target)
                assert edge is not None, f"({owner}, {alias}) unresolvable"
                assert f"`{alias}=`" in book or f"bind({alias}=...)" in book

    def test_no_enum_or_flags_field_documents_empty_values(self) -> None:
        """A field the reader must choose a value for must show the choices.

        The regression this locks out: a bitmask rendered as ``Flags[E]`` looked
        like a bare ``str`` to the doc extractor, so ``scope`` and ``tcp_rules``
        documented no allowed values at all — the one thing a reader opens the
        page for.  An enum or a set of flags must always carry its members.
        """
        offenders: list[str] = []
        for key, pos in _positions().items():
            if not key:
                continue
            cls = _load_class(pos.aci_class)
            for doc in field_docs(cls, _tables().sugar.get(pos.aci_class, {})):
                if doc.enum and not doc.values:
                    offenders.append(f"{key}.{doc.name} (enum {doc.enum})")
        assert not offenders, (
            "enum/flags fields documented with no allowed values: "
            f"{offenders[:10]} — the doc extractor stopped recognising the type."
        )

    def test_no_numeric_field_is_documented_as_a_string(self) -> None:
        """A number must read as a number in the table, never as ``str``.

        The whole overhaul turned schema numbers into ``int``/``float``; the
        reference must say so.  A field whose model annotation is numeric — a
        plain ``int``/``float`` or a named number (``int | Literal[...]``, a port
        the APIC stores under a name) — but which documents as ``str`` means the
        extractor lost the number type.
        """
        offenders: list[str] = []
        for key, pos in _positions().items():
            if not key:
                continue
            cls = _load_class(pos.aci_class)
            for doc in field_docs(cls, _tables().sugar.get(pos.aci_class, {})):
                real = cls.model_fields.get(doc.name)
                if real is not None and _is_numeric(real.annotation) and doc.type_str == "str":
                    offenders.append(f"{key}.{doc.name}")
        assert not offenders, (
            f"numeric fields documented as `str`: {offenders[:10]} — the doc "
            "extractor lost the number type."
        )

    def test_every_enum_used_is_documented(self) -> None:
        """Every enum cited in an attribute table has its section on the page."""
        pages = render_all()
        enums_page = pages["enums.md"]
        cited: set[str] = set()
        for key, pos in _positions().items():
            if not key:
                continue
            cls = _load_class(pos.aci_class)
            for doc in field_docs(cls, _tables().sugar.get(pos.aci_class, {})):
                if doc.enum:
                    cited.add(doc.enum)
        assert cited, "no enum found — the extractor is broken"
        for enum_name in cited:
            assert f"({enum_anchor(enum_name)})=" in enums_page, f"{enum_name} undocumented"
            assert f"## `{enum_name}`" in enums_page


class TestContent:
    def test_generated_banner_on_every_page(self) -> None:
        for content in render_all().values():
            assert content.startswith("<!--\nGenerated by niwaki generate_docs")

    def test_position_page_carries_identity_and_cisco_definition(self) -> None:
        page = render_all()["tenant/tenant-bd.md"]
        assert f"({position_anchor('tenant.bd')})=" in page
        assert "| ACI class | `fvBD` |" in page
        assert "unique layer 2 forwarding domain" in page  # Cisco's own words
        assert "| `arp_flooding` | `arpFlood` | `bool` |" in page

    def test_enum_values_carry_their_meaning(self) -> None:
        enums = render_all()["enums.md"]
        assert "| `bcast` | Broadcast interface |" in enums

    def test_apic_diagnostics_section(self) -> None:
        page = render_all()["tenant/tenant-bd.md"]
        assert "## APIC diagnostics" in page
        assert "FHS-enabled-on-l2-only-bd" in page
        assert "fltFvBDMulticastEnabledOnL2BD" in page

    def test_atomic_classes_carry_the_note(self) -> None:
        pages = render_all()
        atomic_pages = [c for c in pages.values() if "the subtree ships in one request" in c]
        assert atomic_pages, "no atomic position documented"

    def test_coverage_matrix_lists_every_position(self) -> None:
        coverage = render_all()["coverage.md"]
        for key in _positions():
            if not key:
                continue
            assert f"<{position_anchor(key)}>" in coverage, f"{key} missing from the matrix"
        assert "Not curated yet" in coverage

    def test_coverage_total_matches_the_positions(self) -> None:
        coverage = render_all()["coverage.md"]
        rows = len(_positions()) - 1  # every position except the polUni root
        assert f"**{rows} curated positions**" in coverage

    def test_navigation_page_lists_read_side_vocabulary(self) -> None:
        navigation = render_all()["navigation.md"]
        assert "| `tenant` | `.bd(…)` |" in navigation
