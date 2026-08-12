"""niwaki-migrate — rewrite 1.x niwaki call sites to the 2.0 names.

Usage (from any repo using niwaki)::

    uv run python -m tools.niwaki_migrate.migrate PATH [PATH ...] \
        [--dry-run] [--report report.json]

What it rewrites, and what it only flags — the boundary is confidence:

- **Keyword arguments of known calls** (curated design makers and generated
  model constructors): the receiving ACI class is inferred from the call
  name.  A user function that *shadows* a curated maker name AND passes an
  old kwarg will be falsely rewritten — review the diff (every rewrite is
  listed in ``--report``).  Dict literals splatted directly into such a
  call (``**{"addr": ...}``) are rewritten too — the exact shape that is
  invisible to type checkers.
- **Attribute accesses** ``obj.old_name`` for names the table marks
  *attribute-safe*: the old name maps to a single new name across every
  surface AND no class still serves it in 2.0.  Anything else — an old name
  that is still a live name elsewhere (``rtr_id`` lives on ``vnsRtrCfg``) —
  is flagged, never rewritten.
- **Flags only**: ``getattr(x, "old_name")`` (for any distinctive old
  name), string keys the tool cannot attribute to a receiving class, and
  attribute reads of renamed-but-not-safely-rewritable names.  Each flag
  carries file:line; the human decides.  Out of scope by design: 2-3-token
  generic old names in attribute position (``managed_by``) — their
  lookalikes exist in any codebase.

The exit report counts rewrites and flags; the migration's acceptance
metric is ``flags / (rewrites + flags)``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import libcst as cst
from libcst.metadata import CodeRange, MetadataWrapper, PositionProvider

TABLE_PATH = Path(__file__).resolve().parent / "migration_2_0.json"

_CONFLICT = object()


def _load_call_renames(table: dict[str, Any]) -> dict[str, dict[str, object]]:
    """``{call_name: {old_kwarg: new_kwarg | _CONFLICT}}``.

    Call names are curated maker names (mapped to the classes they create)
    and generated model class names (their own constructors).  When two
    classes behind one maker name disagree on a rename, the kwarg is marked
    conflicting and will be flagged instead of rewritten.
    """
    model_fields: dict[str, dict[str, dict[str, str]]] = table["surfaces"]["model_fields"]
    renames_of_class: dict[str, dict[str, str]] = {
        cls: {e["old"]: e["new"] for e in entries.values()} for cls, entries in model_fields.items()
    }
    out: dict[str, dict[str, object]] = {}
    for maker, classes in table["maker_context"].items():
        merged: dict[str, object] = {}
        for cls in classes:
            for old, new in renames_of_class.get(cls, {}).items():
                if old in merged and merged[old] != new:
                    merged[old] = _CONFLICT
                else:
                    merged.setdefault(old, new)
        if merged:
            out[maker] = merged
    for cls, renames in renames_of_class.items():
        if renames:
            out.setdefault(cls, dict(renames))
    return out


@dataclass
class FileReport:
    path: str
    rewrites: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)


class _Migrator(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(
        self,
        call_renames: dict[str, dict[str, object]],
        attribute_safe: dict[str, str],
        attribute_flag: frozenset[str],
        model_old_names: frozenset[str],
        retired_names: frozenset[str],
        report: FileReport,
    ) -> None:
        self._call_renames = call_renames
        self._attribute_safe = attribute_safe
        self._attribute_flag = attribute_flag
        self._model_old = model_old_names
        self._retired = retired_names
        self._report = report
        self._known_splat_dicts: set[int] = set()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _line(self, node: cst.CSTNode) -> int:
        pos = self.get_metadata(PositionProvider, node, None)
        return pos.start.line if isinstance(pos, CodeRange) else 0

    @staticmethod
    def _call_name(node: cst.Call) -> str | None:
        func = node.func
        if isinstance(func, cst.Name):
            return func.value
        if isinstance(func, cst.Attribute):
            return func.attr.value
        return None

    # ── keyword arguments of known calls ──────────────────────────────────────

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        name = self._call_name(updated_node)
        renames = self._call_renames.get(name or "")
        if not renames:
            # Unknown call — a keyword bearing a *retired* old name is
            # migration debt the tool cannot attribute to a class (a local
            # helper forwarding **kwargs to a maker, typically): flag it.
            for arg in updated_node.args:
                if arg.keyword is not None and arg.keyword.value in self._retired:
                    self._report.flags.append(
                        {
                            "line": self._line(original_node),
                            "kind": "kwarg-unknown-call",
                            "detail": f"{name or '<call>'}({arg.keyword.value}=): retired "
                            "1.x name on a call the tool cannot attribute",
                        }
                    )
            return updated_node
        new_args: list[cst.Arg] = []
        for arg in updated_node.args:
            if arg.keyword is not None and arg.keyword.value in renames:
                target = renames[arg.keyword.value]
                if target is _CONFLICT:
                    self._report.flags.append(
                        {
                            "line": self._line(original_node),
                            "kind": "kwarg-conflict",
                            "detail": f"{name}({arg.keyword.value}=): maker maps to "
                            "several classes with diverging renames",
                        }
                    )
                    new_args.append(arg)
                    continue
                self._report.rewrites.append(
                    {
                        "line": self._line(original_node),
                        "kind": "kwarg",
                        "old": arg.keyword.value,
                        "new": target,
                        "call": name,
                    }
                )
                new_args.append(arg.with_changes(keyword=cst.Name(str(target))))
            elif arg.star == "**" and isinstance(arg.value, cst.Dict):
                new_args.append(
                    arg.with_changes(
                        value=self._rewrite_dict(
                            arg.value, name, renames, self._line(original_node)
                        )
                    )
                )
            elif arg.star == "**":
                # A variable splatted into a call whose class we KNOW takes
                # renamed kwargs: the keys are out of sight — the exact shape
                # that slipped past both type checkers in niwaki's own
                # migration.  Always worth a human look.
                self._report.flags.append(
                    {
                        "line": self._line(original_node),
                        "kind": "opaque-splat",
                        "detail": f"{name}(**...): keys built elsewhere — verify them "
                        "against the migration table",
                    }
                )
                new_args.append(arg)
            else:
                new_args.append(arg)
        return updated_node.with_changes(args=new_args)

    def _rewrite_dict(
        self, node: cst.Dict, call: str | None, renames: dict[str, object], line: int
    ) -> cst.Dict:
        elements: list[cst.BaseDictElement] = []
        for el in node.elements:
            if (
                isinstance(el, cst.DictElement)
                and isinstance(el.key, cst.SimpleString)
                and el.key.evaluated_value in renames
            ):
                target = renames[el.key.evaluated_value]
                if target is _CONFLICT:
                    self._report.flags.append(
                        {
                            "line": line,
                            "kind": "dictkey-conflict",
                            "detail": f"**{{...}} into {call}: {el.key.evaluated_value!r}",
                        }
                    )
                    elements.append(el)
                    continue
                # .quote, never value[0]: a prefixed key (r"...") puts the
                # prefix letter first, and rebuilding with it as the "quote"
                # raises CSTValidationError mid-run — after some files were
                # already rewritten.  The prefix itself is preserved.
                quote = el.key.quote
                self._report.rewrites.append(
                    {
                        "line": line,
                        "kind": "dictkey",
                        "old": el.key.evaluated_value,
                        "new": target,
                        "call": call,
                    }
                )
                elements.append(
                    el.with_changes(key=cst.SimpleString(f"{el.key.prefix}{quote}{target}{quote}"))
                )
            else:
                if (
                    isinstance(el, cst.DictElement)
                    and isinstance(el.key, cst.SimpleString)
                    and el.key.evaluated_value in self._retired
                ):
                    self._report.flags.append(
                        {
                            "line": line,
                            "kind": "dictkey-retired-other-class",
                            "detail": f"**{{...}} into {call}: {el.key.evaluated_value!r} "
                            "is a retired 1.x name but not a rename of this maker's "
                            "class — wrong key or wrong maker",
                        }
                    )
                elements.append(el)
        return node.with_changes(elements=elements)

    # ── attribute accesses ────────────────────────────────────────────────────

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.Attribute:
        attr = updated_node.attr.value
        target = self._attribute_safe.get(attr)
        if target is not None:
            self._report.rewrites.append(
                {"line": self._line(original_node), "kind": "attribute", "old": attr, "new": target}
            )
            return updated_node.with_changes(attr=cst.Name(target))
        if attr in self._attribute_flag:
            self._report.flags.append(
                {
                    "line": self._line(original_node),
                    "kind": "attribute-flag",
                    "detail": f".{attr}: renamed in 2.0 but not safely rewritable "
                    "(multi-target or still live on another class) — resolve by "
                    "receiver class",
                }
            )
        elif attr in self._model_old and attr.count("_") >= 1:
            self._report.flags.append(
                {
                    "line": self._line(original_node),
                    "kind": "attribute-ambiguous",
                    "detail": f".{attr}: renamed on some classes but still a live "
                    "name elsewhere — resolve by receiver class",
                }
            )
        return updated_node

    # ── retired names in string positions ─────────────────────────────────────

    def visit_Subscript(self, node: cst.Subscript) -> None:
        for el in node.slice:
            idx = el.slice
            if (
                isinstance(idx, cst.Index)
                and isinstance(idx.value, cst.SimpleString)
                and idx.value.evaluated_value in self._retired
            ):
                self._report.flags.append(
                    {
                        "line": self._line(node),
                        "kind": "subscript",
                        "detail": f"[...{idx.value.evaluated_value!r}]: retired 1.x name "
                        "used as a key",
                    }
                )

    def visit_Dict(self, node: cst.Dict) -> None:
        if id(node) in self._known_splat_dicts:
            return  # rewritten (or conflict-flagged) by the enclosing call
        for el in node.elements:
            if (
                isinstance(el, cst.DictElement)
                and isinstance(el.key, cst.SimpleString)
                and el.key.evaluated_value in self._retired
            ):
                self._report.flags.append(
                    {
                        "line": self._line(node),
                        "kind": "dictkey-unattributed",
                        "detail": f"{el.key.evaluated_value!r}: retired 1.x name as a "
                        "dict key outside a known call",
                    }
                )

    # ── getattr with a literal old name ───────────────────────────────────────

    def visit_Call(self, node: cst.Call) -> None:
        if self._call_name(node) in self._call_renames:
            for arg in node.args:
                if arg.star == "**" and isinstance(arg.value, cst.Dict):
                    self._known_splat_dicts.add(id(arg.value))
        if (
            isinstance(node.func, cst.Name)
            and node.func.value == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1].value, cst.SimpleString)
            and node.args[1].value.evaluated_value
            in (self._model_old | self._attribute_flag | set(self._attribute_safe))
        ):
            self._report.flags.append(
                {
                    "line": self._line(node),
                    "kind": "getattr",
                    "detail": f"getattr(..., {node.args[1].value.evaluated_value!r})",
                }
            )


def migrate_source(
    source: str, table: dict[str, Any], path: str = "<memory>"
) -> tuple[str, FileReport]:
    """Rewrite one file's source; return (new_source, report)."""
    call_renames = _load_call_renames(table)
    attribute_safe = dict(table["attribute_safe"])
    all_old: set[str] = set()
    for surface in ("model_fields", "catalog_readable"):
        for entries in table["surfaces"][surface].values():
            all_old.update(e["old"] for e in entries.values())
    # Attribute rewrites use attribute_safe (unique + dead + >=4 tokens);
    # attribute_flag carries every other distinctive old name (>=4 tokens)
    # the tool refuses to rewrite blindly.  2-3-token generic names
    # (managed_by, mounted_on) are deliberately neither rewritten nor
    # flagged: their lookalikes exist in any Python codebase.
    model_old: set[str] = set()
    for entries in table["surfaces"]["model_fields"].values():
        model_old.update(e["old"] for e in entries.values())
    report = FileReport(path=path)
    wrapper = MetadataWrapper(cst.parse_module(source))
    migrator = _Migrator(
        call_renames,
        attribute_safe,
        frozenset(table.get("attribute_flag", [])),
        frozenset(model_old),
        frozenset(table.get("retired_kwarg_names", [])),
        report,
    )
    new_module = wrapper.visit(migrator)
    return new_module.code, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="niwaki-migrate", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    table = json.loads(TABLE_PATH.read_text())
    excluded = {".venv", "venv", ".git", "__pycache__", "node_modules", ".tox", ".eggs"}
    files: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            files.extend(
                f for f in sorted(p.rglob("*.py")) if not (excluded & set(part for part in f.parts))
            )
        else:
            files.append(p)

    reports: list[FileReport] = []
    n_rewrites = n_flags = n_files_changed = 0
    for f in files:
        try:
            source = f.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  SKIP (unreadable: {type(exc).__name__}): {f}", file=sys.stderr)
            continue
        try:
            new_source, report = migrate_source(source, table, str(f))
        except cst.ParserSyntaxError:
            print(f"  SKIP (unparsable): {f}", file=sys.stderr)
            continue
        reports.append(report)
        n_rewrites += len(report.rewrites)
        n_flags += len(report.flags)
        if new_source != source:
            n_files_changed += 1
            if not args.dry_run:
                f.write_text(new_source)

    total = n_rewrites + n_flags
    pct = (100.0 * n_flags / total) if total else 0.0
    print(
        f"niwaki-migrate: {len(files)} files scanned, {n_files_changed} changed, "
        f"{n_rewrites} rewrites, {n_flags} flagged ({pct:.1f}% manual)"
    )
    for r in reports:
        for flag in r.flags:
            print(f"  FLAG {r.path}:{flag['line']} [{flag['kind']}] {flag['detail']}")
    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "rewrites": n_rewrites,
                    "flags": n_flags,
                    "manual_pct": pct,
                    "files": [
                        {"path": r.path, "rewrites": r.rewrites, "flags": r.flags}
                        for r in reports
                        if r.rewrites or r.flags
                    ],
                },
                indent=1,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
