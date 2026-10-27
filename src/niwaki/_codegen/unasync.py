"""Generate the sync twin of an async module — the unasync rewriter.

Some sync/async pairs in niwaki are written once, async, and their sync twin is
**produced** from that source instead of being maintained by hand.  The
generated files are committed (they are the code sync users run, measured by
coverage like any other module) and open with a ``DO NOT EDIT`` header: a fix
lands in the async source, then ``--write`` regenerates the twin.

How a module is rewritten
-------------------------

:func:`render` is a token rewriter built on :mod:`tokenize` — never on a
re-printed AST, so every line keeps its layout and comments:

- ``async`` and ``await`` are dropped from the code (``async def`` /
  ``async with`` / ``async for`` / async comprehensions become plain, an
  awaited expression loses its ``await``);
- :data:`RULES` — the rename table shared with the parity guard — maps the
  async vocabulary onto the sync one in the code, in comments and inside
  string literals (docstrings and the literal parts of f-strings included);
- inside string literals, docstring examples are rendered as sync code too:
  ``await`` and the whitespace after it are dropped, and so is ``async``
  before ``for`` / ``with`` / ``def``.

A line with nothing async in it is preserved byte for byte.  The result is
then passed through ``ruff format`` with the repository's configuration.

Markers
-------

A real semantic difference between the twins is expressed by a marker — a
comment whose text is ``unasync: <directive>``.  The language is deliberately
minimal (three directives) and every marker is consumed:

``# unasync: async-only``
    The physical line carrying the marker is dropped.  On a comment-only line
    only that comment line goes; it never reaches the next statement.

``# unasync: async-only-begin`` … ``# unasync: async-only-end``
    Each on a line of its own.  Every line from the begin marker to the end
    marker, both included, is dropped.  Blocks do not nest.

``# unasync: sync: <code>``
    The physical line carrying the marker is replaced by ``<code>``,
    **verbatim** (no rule applied: it is already sync code), indented like
    that line.  On a comment-only line this inserts a sync-only line.

Fail-loud
---------

Every inconsistency raises :class:`UnasyncError`: an unknown directive, an
unbalanced or nested block, an empty ``sync:`` replacement, and any async
residue in the code of the output — an ``async`` / ``await`` keyword, an
``asyncio`` import, an identifier starting with ``Async`` that no rule renamed
— or an output that is not valid Python.

Why it is not in the regen manifest
-----------------------------------

The generator is corpus-free: it reads the async sources listed in
:data:`PAIRS`, never the schema corpus.  Coupling it to the regen freshness
manifest (:mod:`niwaki._codegen.freshness`) would force a corpus regen on
every async edit; its own guard (``--check``, run by ``scripts/checks.sh
--fast`` and ``tests/test_unasync_fresh.py``) covers it instead.

Usage::

    uv run python -m niwaki._codegen.unasync --write   # regenerate the twins
    uv run python -m niwaki._codegen.unasync --check   # exit 1 when one is stale
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Final

from niwaki._codegen.twins import RULES

__all__ = ["PAIRS", "RULES", "UnasyncError", "main", "render"]

PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("query/_async_builder.py", "query/_builder.py"),
    ("query/_async_subscription.py", "query/_subscription.py"),
)
"""The generated pairs, as ``(async_source, sync_target)`` paths.

Both paths are POSIX-style and relative to the package directory
(``src/niwaki`` by default, see :func:`main`).  The subscription socket is
deliberately absent: threads versus tasks make its twins differ by design, so
they stay hand-written under the parity guard.
"""

_PACKAGE: Final[Path] = Path(__file__).resolve().parents[1]
_REPO: Final[Path] = _PACKAGE.parents[1]

_MARKER: Final[re.Pattern[str]] = re.compile(r"#\s*unasync:(?P<directive>.*)\Z")
_LINE: Final[re.Pattern[str]] = re.compile(r"[^\n]*\n|[^\n]+\Z")
_SYNC_PREFIX: Final[str] = "sync:"

# Inside a string literal, an example written for async users renders as sync
# code.  The whitespace class excludes newlines so no line is ever merged.
_STRING_DROPS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bawait\b[ \t]+"),
    re.compile(r"\basync[ \t]+(?=(?:for|with|def)\b)"),
)
# In code, ``async`` / ``await`` can only be keywords (a string or a comment
# never reaches this pattern), so a textual drop is exact.
_CODE_DROP: Final[re.Pattern[str]] = re.compile(r"\b(?:async|await)\b[ \t]*")


class UnasyncError(ValueError):
    """The async source cannot be rewritten into a correct sync twin.

    Raised by :func:`render` for every fail-loud condition: an unknown marker
    directive, an unbalanced or nested ``async-only-begin`` /
    ``async-only-end`` block, an empty ``sync:`` replacement, async residue in
    the code of the output, or an output that is not valid Python.  The
    message names the cause (the directive, the residual keyword or name) and,
    when there is one, the source line.
    """


# ── Markers ───────────────────────────────────────────────────────────────────


class _Action(Enum):
    """What happens to one physical line of the async source."""

    KEEP = auto()
    DROP = auto()
    REPLACE = auto()


@dataclass(frozen=True, slots=True)
class _LinePlan:
    """The fate of one physical line: kept (rewritten), dropped or replaced."""

    action: _Action
    code: str = ""


def _tokens(source: str, *, origin: str) -> list[tokenize.TokenInfo]:
    """Tokenize ``source``, turning a tokenizer failure into :class:`UnasyncError`."""
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise UnasyncError(f"{origin}: not valid Python ({exc})") from exc


def _physical_lines(source: str) -> list[str]:
    """Split on ``\\n`` only, like the tokenizer, keeping the line ends.

    :meth:`str.splitlines` would also split on form feeds and other Unicode
    line boundaries the tokenizer does not count, shifting every row after
    them.
    """
    return _LINE.findall(source)


def _plan_lines(
    tokens: Sequence[tokenize.TokenInfo], lines: Sequence[str], *, origin: str
) -> dict[int, _LinePlan]:
    """Read every marker comment and decide the fate of the lines it governs.

    Returns:
        ``{line index (0-based): plan}`` for every line a marker touches;
        absent lines are kept.

    Raises:
        UnasyncError: An unknown directive, an empty ``sync:`` replacement, a
            block marker sharing its line with code, or an unbalanced or
            nested block.
    """
    plans: dict[int, _LinePlan] = {}
    open_block: int | None = None
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        match = _MARKER.match(token.string)
        if match is None:
            continue
        index = token.start[0] - 1
        line = lines[index]
        directive = match["directive"].strip()
        comment_only = not line[: token.start[1]].strip()
        where = f"{origin}:{index + 1}"
        if directive in {"async-only-begin", "async-only-end"} and not comment_only:
            raise UnasyncError(f"{where}: '{directive}' must sit on a line of its own")
        if directive == "async-only-begin":
            if open_block is not None:
                raise UnasyncError(
                    f"{where}: nested 'async-only-begin' (a block is already open "
                    f"at line {open_block + 1})"
                )
            open_block = index
        elif directive == "async-only-end":
            if open_block is None:
                raise UnasyncError(f"{where}: 'async-only-end' without an 'async-only-begin'")
            for dropped in range(open_block, index + 1):
                plans[dropped] = _LinePlan(_Action.DROP)
            open_block = None
        elif directive == "async-only":
            plans[index] = _LinePlan(_Action.DROP)
        elif directive.startswith(_SYNC_PREFIX):
            code = directive.removeprefix(_SYNC_PREFIX).strip()
            if not code:
                raise UnasyncError(f"{where}: empty 'sync:' replacement")
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            plans[index] = _LinePlan(_Action.REPLACE, f"{indent}{code}\n")
        else:
            raise UnasyncError(f"{where}: unknown unasync directive {directive!r}")
    if open_block is not None:
        raise UnasyncError(
            f"{origin}:{open_block + 1}: 'async-only-begin' without an 'async-only-end'"
        )
    return plans


# ── The rewrite ───────────────────────────────────────────────────────────────


def _apply_rules(text: str) -> str:
    """Apply :data:`RULES` once, in order — the parity guard's exact renaming."""
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text


def _rewrite_code(text: str) -> str:
    return _apply_rules(_CODE_DROP.sub("", text))


def _rewrite_string(text: str) -> str:
    for pattern in _STRING_DROPS:
        text = pattern.sub("", text)
    return _apply_rules(text)


def _offsets(lines: Sequence[str]) -> list[int]:
    """The character offset at which each physical line starts."""
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    return starts


def _rewrite(source: str, tokens: Sequence[tokenize.TokenInfo], lines: Sequence[str]) -> str:
    """Rewrite ``source`` region by region; the newline count never changes.

    String literals (an f-string as a whole, from its start to its matching
    end) and comments are the special regions; everything between them is
    code.  Each region gets its own transform, none of which can add or
    remove a newline, so the output's physical lines map one-to-one onto the
    input's — which is what lets the markers act line by line afterwards.
    """
    starts = _offsets(lines)

    def offset(position: tuple[int, int]) -> int:
        row, column = position
        return starts[row - 1] + column

    regions: list[tuple[int, int, bool]] = []  # (start, end, is_string)
    fstring_depth = 0
    fstring_start = 0
    for token in tokens:
        match token.type:
            case tokenize.FSTRING_START:
                if fstring_depth == 0:
                    fstring_start = offset(token.start)
                fstring_depth += 1
            case tokenize.FSTRING_END:
                fstring_depth -= 1
                if fstring_depth == 0:
                    regions.append((fstring_start, offset(token.end), True))
            case tokenize.STRING if fstring_depth == 0:
                regions.append((offset(token.start), offset(token.end), True))
            case tokenize.COMMENT if fstring_depth == 0:
                regions.append((offset(token.start), offset(token.end), False))
            case _:
                pass

    out: list[str] = []
    cursor = 0
    for start, end, is_string in regions:
        out.append(_rewrite_code(source[cursor:start]))
        region = source[start:end]
        out.append(_rewrite_string(region) if is_string else _apply_rules(region))
        cursor = end
    out.append(_rewrite_code(source[cursor:]))
    return "".join(out)


# ── Residue ───────────────────────────────────────────────────────────────────


def _check_residue(output: str, *, origin: str) -> None:
    """Refuse an output whose *code* still carries anything async.

    Raises:
        UnasyncError: An ``async`` / ``await`` keyword, an ``Async*`` name, an
            output that is not valid Python, or an ``asyncio`` import.
    """
    for token in _tokens(output, origin=f"{origin} (generated)"):
        if token.type != tokenize.NAME:
            continue
        where = f"{origin} (generated line {token.start[0]})"
        if token.string in {"async", "await"}:
            raise UnasyncError(f"{where}: residual '{token.string}' keyword")
        if token.string.startswith("Async"):
            raise UnasyncError(
                f"{where}: residual Async* name {token.string!r} — no rule renames it"
            )
    try:
        tree = ast.parse(output)
    except SyntaxError as exc:
        raise UnasyncError(f"{origin}: the generated twin is not valid Python ({exc})") from exc
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names) if any(
                alias.name.partition(".")[0] == "asyncio" for alias in names
            ):
                raise UnasyncError(
                    f"{origin} (generated line {node.lineno}): residual 'import asyncio'"
                )
            case ast.ImportFrom(module=str(module), level=0) if (
                module.partition(".")[0] == "asyncio"
            ):
                raise UnasyncError(
                    f"{origin} (generated line {node.lineno}): residual 'from asyncio import'"
                )
            case _:
                pass


def _header(origin: str) -> str:
    return (
        f"# GENERATED from {origin} — DO NOT EDIT.\n"
        "# Edit the async source, then run: uv run python -m niwaki._codegen.unasync --write\n"
    )


def render(source: str, *, origin: str) -> str:
    """Rewrite an async module's source into its sync twin's source.

    A pure function: no file is read, nothing is formatted (``ruff format``
    is :func:`main`'s business).  The rewrite is deterministic, and
    idempotent on its own output once the header is removed.

    Args:
        source: The async module's source text.
        origin: The async source's path as shown to a reader of the generated
            file — echoed verbatim in the ``DO NOT EDIT`` header and in error
            messages (:func:`main` passes ``src/niwaki/<async path>``).

    Returns:
        The sync module's source: a comment header whose first line starts
        with ``# GENERATED from {origin} — DO NOT EDIT`` (comments, so the
        module docstring stays first), then the rewritten module.

    Raises:
        UnasyncError: ``source`` is not valid Python, a marker is unknown,
            unbalanced, nested or empty, or the output still carries async
            code (see the module docstring) or is not valid Python.

    Example::

        >>> print(render("async def f():\\n    await asyncio.sleep(1)\\n", origin="a.py"))
        # GENERATED from a.py — DO NOT EDIT.
        # Edit the async source, then run: uv run python -m niwaki._codegen.unasync --write
        def f():
            time.sleep(1)
        <BLANKLINE>
    """
    tokens = _tokens(source, origin=origin)
    lines = _physical_lines(source)
    plans = _plan_lines(tokens, lines, origin=origin)
    rewritten = _physical_lines(_rewrite(source, tokens, lines))
    if len(rewritten) != len(lines):  # pragma: no cover - the rewrite keeps every newline
        raise UnasyncError(f"{origin}: the rewrite changed the line count")
    body: list[str] = []
    for index, line in enumerate(rewritten):
        plan = plans.get(index, _LinePlan(_Action.KEEP))
        match plan.action:
            case _Action.KEEP:
                body.append(line)
            case _Action.REPLACE:
                body.append(plan.code)
            case _Action.DROP:
                pass
    output = _header(origin) + "".join(body)
    _check_residue(output, origin=origin)
    return output


# ── Command line ──────────────────────────────────────────────────────────────


def _ruff_format(text: str, target: Path) -> str:
    """Format ``text`` exactly as ``ruff format`` formats ``target`` in this repository.

    The repository's ``pyproject.toml`` is passed explicitly, so a target
    outside the source tree (a scratch ``--root``) is formatted with the same
    configuration as the committed files.
    """
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(_REPO / "pyproject.toml"),
            "--stdin-filename",
            str(target),
            "-",
        ],
        input=text,
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO,
    )
    if done.returncode != 0:
        raise UnasyncError(f"ruff format failed on {target}: {done.stderr.strip()}")
    return done.stdout


def _expected(root: Path, async_path: str, sync_path: str) -> str:
    source = (root / async_path).read_text(encoding="utf-8")
    return _ruff_format(render(source, origin=f"src/niwaki/{async_path}"), root / sync_path)


def main(argv: Sequence[str] | None = None) -> int:
    """Regenerate (``--write``) or verify (``--check``) every pair of :data:`PAIRS`.

    Args:
        argv: Command-line arguments (``sys.argv[1:]`` when ``None``).
            Exactly one of ``--check`` / ``--write`` is required;
            ``--root DIR`` is the package directory the :data:`PAIRS` paths
            are relative to (default: this source tree's ``src/niwaki``).

    Returns:
        ``0`` when every target was written (``--write``) or is fresh
        (``--check``); ``1`` when a target is stale or missing (``--check``,
        nothing is written) or a source cannot be rewritten — each problem is
        named on stderr.

    Raises:
        SystemExit: The arguments are invalid (argparse).

    Example::

        uv run python -m niwaki._codegen.unasync --check
    """
    parser = argparse.ArgumentParser(
        prog="python -m niwaki._codegen.unasync",
        description="Generate the sync twins of the async modules listed in PAIRS.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when a twin is stale")
    mode.add_argument("--write", action="store_true", help="regenerate every twin")
    parser.add_argument(
        "--root", type=Path, default=_PACKAGE, help="package directory (default: src/niwaki)"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    status = 0
    for async_path, sync_path in PAIRS:
        try:
            expected = _expected(root, async_path, sync_path)
        except UnasyncError as exc:
            print(f"[unasync] {sync_path}: {exc}", file=sys.stderr)
            status = 1
            continue
        target = root / sync_path
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current == expected:
            continue
        if args.write:
            target.write_text(expected, encoding="utf-8", newline="")
            print(f"[unasync] wrote {sync_path}")
        else:
            state = "missing" if current is None else "stale"
            print(
                f"[unasync] {sync_path} is {state} — edit {async_path}, then run "
                "`uv run python -m niwaki._codegen.unasync --write`",
                file=sys.stderr,
            )
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
