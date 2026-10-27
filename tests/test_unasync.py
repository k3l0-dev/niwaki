"""The unasync generator — the sync twin is *produced* from its async source.

The query builder and the query subscription are written once, async, and
their sync twins are generated (``src/niwaki/query/_builder.py`` and
``src/niwaki/query/_subscription.py`` are committed outputs, never edited by
hand).  This module pins the generator engine; the freshness of the committed
outputs is ``tests/test_unasync_fresh.py``, the one-shot migration proof and
the docstring contract are ``tests/query/test_generated_twins.py``.

The generator — ``niwaki._codegen.unasync`` (API contract)
-----------------------------------------------------------

A stdlib-only token rewriter (``tokenize``, plus regular expressions inside
string literals and comments).  Corpus-free: it reads no schema, so it stays
outside the regen freshness manifest and runs in ``checks.sh --fast``.

``RULES``
    **The very object** :data:`niwaki._codegen.twins.RULES` — the parity guard
    and the generator share one rename table, so a generated twin is compared
    under exactly the rules it was made with.

``PAIRS: Final[tuple[tuple[str, str], ...]]``
    ``(async_source, sync_target)`` paths relative to ``src/niwaki``.  This
    lot generates exactly the two query pairs
    (``query/_async_builder.py`` -> ``query/_builder.py`` and
    ``query/_async_subscription.py`` -> ``query/_subscription.py``).

``class UnasyncError(ValueError)``
    Raised for every fail-loud condition below; the message names the cause.

``render(source: str, *, origin: str) -> str``
    Pure function: async module source in, sync module source out (not yet
    passed through ``ruff format``).

    1. **Header.**  The output opens with a block of comment lines whose first
       line starts with ``# GENERATED from {origin} — DO NOT EDIT`` (``origin``
       echoed verbatim; the generator passes ``src/niwaki/<async path>``).
       Being comments, the header leaves the module docstring first.
    2. **Async syntax removed** from code: ``async def`` / ``async with`` /
       ``async for`` / async comprehensions lose ``async``; ``await`` is
       dropped from its expression.
    3. **RULES applied** to the code text (identifiers, dotted names such as
       ``asyncio.sleep``, module paths), to the contents of string literals
       (docstrings and the literal parts of f-strings included — escaped
       ``{{`` / ``}}`` survive) and to comments.
    4. **Docstring examples render as sync code**: inside string literals,
       ``await `` (the word and the whitespace after it) is dropped, and
       ``async `` before ``for`` / ``with`` / ``def`` is dropped.
    5. A line with nothing async in it is preserved byte for byte.
    6. **Markers** — a comment whose text is ``unasync: <directive>``.  There
       are exactly three directives, and markers are consumed (no
       ``unasync:`` comment survives in the output):

       - ``# unasync: async-only`` — its own physical line is dropped (a
         trailing marker drops the code line; on a comment-only line it drops
         that line only — it never reaches the next statement);
       - ``# unasync: async-only-begin`` … ``# unasync: async-only-end`` — each
         on a line of its own; every line from begin to end, both markers
         included, is dropped.  An end without a begin, a begin without an
         end, or a begin inside an open block (nesting) is an
         :class:`UnasyncError`;
       - ``# unasync: sync: <code>`` — the physical line carrying the marker
         is replaced by ``<code>``, **verbatim** (no rule applied — it is
         already sync code), indented like that line.  On a comment-only line
         this *inserts* a sync-only line.  An empty ``<code>`` is an
         :class:`UnasyncError`.

       Any other directive is an :class:`UnasyncError` naming it.  Marker-like
       text inside a string literal is not a marker.
    7. **Fail-loud on residue** (:class:`UnasyncError`), checked on the code
       of the output: an ``async`` or ``await`` keyword (only a ``sync:``
       marker can smuggle one in), an ``import asyncio`` /
       ``from asyncio import …`` statement, an identifier starting with
       ``Async`` that no rule renamed (``AsyncioRunner``), or an output that is
       not valid Python.
    8. **Deterministic and idempotent**: equal inputs give equal bytes, and
       re-rendering a generated body (header stripped) is a fixed point.

``main(argv: Sequence[str] | None = None) -> int``
    ``python -m niwaki._codegen.unasync --check | --write [--root DIR]``.
    Exactly one of ``--check`` / ``--write`` (argparse exits otherwise).
    ``--root`` is the package directory the ``PAIRS`` paths are relative to
    (default: this source tree's ``src/niwaki``).  Each target is
    ``render(...)`` passed through ``ruff format`` **with the repository's
    ruff configuration**, whatever the root.  ``--write`` writes every target
    and returns 0 (a second run changes no byte); ``--check`` writes nothing,
    returns 0 when every target is fresh, and 1 when one is stale or missing,
    naming it on stdout or stderr.
"""

from __future__ import annotations

import ast
import shutil
import textwrap
from pathlib import Path

import pytest

from niwaki._codegen import twins, unasync

SRC = Path(__file__).resolve().parents[1] / "src" / "niwaki"
ORIGIN = "src/niwaki/query/_async_example.py"


def _src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _body(output: str) -> str:
    """The generated source without its header comment block (and the blank
    lines that may separate it from the source)."""
    lines = output.splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].startswith("#"):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "".join(lines[index:])


def _render_body(text: str) -> str:
    return _body(unasync.render(_src(text), origin=ORIGIN))


def _same_tree(left: str, right: str) -> bool:
    return ast.dump(ast.parse(left)) == ast.dump(ast.parse(_src(right)))


# ── The shared table and the pair list ────────────────────────────────────────


def test_the_generator_shares_the_parity_guard_rule_table() -> None:
    assert unasync.RULES is twins.RULES


def test_unasync_error_is_a_value_error() -> None:
    assert issubclass(unasync.UnasyncError, ValueError)


def test_this_lot_generates_the_two_query_pairs() -> None:
    assert set(unasync.PAIRS) >= {
        ("query/_async_builder.py", "query/_builder.py"),
        ("query/_async_subscription.py", "query/_subscription.py"),
    }


# ── RULES, one by one (renames never touch whitespace: exact text) ────────────


@pytest.mark.parametrize(
    ("async_line", "sync_line"),
    [
        # 1. StopAsyncIteration
        ("raise StopAsyncIteration\n", "raise StopIteration\n"),
        # 2. asynccontextmanager
        (
            "from contextlib import asynccontextmanager\n",
            "from contextlib import contextmanager\n",
        ),
        # 3. the four async dunders
        ("x = obj.__aenter__\n", "x = obj.__enter__\n"),
        ("x = obj.__aexit__\n", "x = obj.__exit__\n"),
        ("x = obj.__aiter__\n", "x = obj.__iter__\n"),
        ("x = obj.__anext__\n", "x = obj.__next__\n"),
        # 4. aclose
        ("x = client.aclose\n", "x = client.close\n"),
        # 5. asyncio.sleep, a dotted name spanning several tokens
        ("pause = asyncio.sleep\n", "pause = time.sleep\n"),
        # 6. private async iterators
        ("pages = self._session._aiter_pages\n", "pages = self._session._iter_pages\n"),
        # 7. the façade's session accessor
        ("s = self._active_session\n", "s = self._sync_session\n"),
        # 8. the Async class-name prefix, public and private
        ("s: AsyncApicSession\n", "s: ApicSession\n"),
        ("r: _AsyncRegistration\n", "r: _Registration\n"),
        ("it: AsyncIterator[int]\n", "it: Iterator[int]\n"),
        # 9. module infix
        (
            "from niwaki.query._async_builder import AsyncQuery\n",
            "from niwaki.query._builder import Query\n",
        ),
        # 10. module / function suffix
        (
            "from niwaki.transport.session_async import AsyncApicSession\n",
            "from niwaki.transport.session import ApicSession\n",
        ),
        ("result = push_async(design)\n", "result = push(design)\n"),
        # 11. the sync suffix of shared shells
        ("result = push_sync(design)\n", "result = push(design)\n"),
    ],
)
def test_each_rule_renames_in_code(async_line: str, sync_line: str) -> None:
    assert _render_body(async_line) == sync_line


def test_a_line_with_nothing_async_is_preserved_byte_for_byte() -> None:
    source = 'x  =  {"a":1}   # keep   me\ny = [ 1,2 ]\n'
    assert _render_body(source) == source


# ── Async syntax removal (compared as trees: whitespace is ruff's business) ───


def test_async_def_and_await_are_removed() -> None:
    out = _render_body(
        """
        async def fetch(self):
            raw = await self._session.get()
            return await parse(raw)
        """
    )
    assert _same_tree(
        out,
        """
        def fetch(self):
            raw = self._session.get()
            return parse(raw)
        """,
    )


def test_async_with_and_async_for_become_plain() -> None:
    out = _render_body(
        """
        async def run(self):
            async with self._lock:
                async for page in self._session._aiter_pages():
                    yield page
        """
    )
    assert _same_tree(
        out,
        """
        def run(self):
            with self._lock:
                for page in self._session._iter_pages():
                    yield page
        """,
    )


def test_an_async_comprehension_becomes_plain() -> None:
    out = _render_body("async def f(self):\n    return [obj async for obj in self.stream()]\n")
    assert _same_tree(out, "def f(self):\n    return [obj for obj in self.stream()]\n")


def test_awaiting_an_async_dunder_becomes_the_sync_dunder_call() -> None:
    out = _render_body("async def nxt(self):\n    return await self._raw.__anext__()\n")
    assert _same_tree(out, "def nxt(self):\n    return self._raw.__next__()\n")


def test_decorators_annotations_and_type_params_survive() -> None:
    out = _render_body(
        """
        class AsyncBox[T: int]:
            @property
            def value(self) -> T:
                return self._v

            async def __aenter__(self) -> AsyncBox[T]:
                return self

            async def __aexit__(self, *exc: object) -> None:
                await self.aclose()
        """
    )
    assert _same_tree(
        out,
        """
        class Box[T: int]:
            @property
            def value(self) -> T:
                return self._v

            def __enter__(self) -> Box[T]:
                return self

            def __exit__(self, *exc: object) -> None:
                self.close()
        """,
    )


# ── Strings and comments ──────────────────────────────────────────────────────


def test_rules_apply_inside_docstrings() -> None:
    out = _render_body('def f():\n    """Return an AsyncQuery bound to AsyncApicSession."""\n')
    assert '"""Return an Query bound to ApicSession."""' in out


def test_docstring_examples_render_as_sync_code() -> None:
    out = _render_body(
        '''
        def f():
            """Example::

                async with AsyncNiwaki(url) as aci:
                    bds = await aci.query(fvBD).fetch()
                    async for bd in aci.query(fvBD).stream():
                        await process(bd)
            """
        '''
    )
    assert "        with Niwaki(url) as aci:\n" in out
    assert "            bds = aci.query(fvBD).fetch()\n" in out
    assert "            for bd in aci.query(fvBD).stream():\n" in out
    assert "                process(bd)\n" in out
    assert "await" not in out
    assert "async" not in out


def test_rules_apply_inside_f_strings_and_escaped_braces_survive() -> None:
    out = _render_body('msg = f"{AsyncQuery.__name__} uses {{braces}} via _aiter_pages"\n')
    assert out == 'msg = f"{Query.__name__} uses {{braces}} via _iter_pages"\n'


def test_rules_apply_inside_comments() -> None:
    out = _render_body("x = 1  # AsyncQuery mirrors it\n")
    assert out == "x = 1  # Query mirrors it\n"


def test_marker_like_text_inside_a_string_is_not_a_marker() -> None:
    source = 'x = "# unasync: bogus"\n'
    assert _render_body(source) == source


# ── Marker 1: async-only (its own line, nothing else) ─────────────────────────


def test_async_only_drops_the_marked_line() -> None:
    out = _render_body(
        """
        from typing import cast
        from typing import Never  # unasync: async-only
        x = cast(int, 1)
        """
    )
    assert out == "from typing import cast\nx = cast(int, 1)\n"


def test_async_only_on_a_comment_line_drops_only_that_line() -> None:
    out = _render_body(
        """
        # unasync: async-only
        x = 1
        """
    )
    assert out == "x = 1\n"


# ── Marker 2: async-only-begin / async-only-end ───────────────────────────────


def test_a_begin_end_block_is_dropped_with_its_markers() -> None:
    out = _render_body(
        """
        class AsyncQuery:
            def __aiter__(self) -> AsyncIterator[int]:
                return self.stream()

            # unasync: async-only-begin
            def __iter__(self) -> Never:
                raise TypeError("AsyncQuery is not synchronously iterable")
            # unasync: async-only-end

            async def first(self) -> int:
                return await self.one()
        """
    )
    assert "unasync" not in out
    assert "TypeError" not in out
    assert _same_tree(
        out,
        """
        class Query:
            def __iter__(self) -> Iterator[int]:
                return self.stream()

            def first(self) -> int:
                return self.one()
        """,
    )


def test_an_end_without_a_begin_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="async-only-end"):
        unasync.render("x = 1\n# unasync: async-only-end\n", origin=ORIGIN)


def test_a_begin_without_an_end_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="async-only-begin"):
        unasync.render("# unasync: async-only-begin\nx = 1\n", origin=ORIGIN)


def test_a_nested_begin_is_an_error() -> None:
    source = _src(
        """
        # unasync: async-only-begin
        x = 1
        # unasync: async-only-begin
        y = 2
        # unasync: async-only-end
        # unasync: async-only-end
        """
    )
    with pytest.raises(unasync.UnasyncError, match="async-only-begin"):
        unasync.render(source, origin=ORIGIN)


# ── Marker 3: sync: <code> ────────────────────────────────────────────────────


def test_sync_replaces_the_marked_line_keeping_its_indentation() -> None:
    out = _render_body(
        """
        import asyncio  # unasync: sync: import time


        class AsyncS:
            async def wait(self):
                await asyncio.sleep(1)  # unasync: sync: time.sleep(1)
        """
    )
    assert out.startswith("import time\n")
    assert "        time.sleep(1)\n" in out
    assert "asyncio" not in out


def test_sync_on_a_comment_line_inserts_a_sync_only_line() -> None:
    out = _render_body(
        """
        def close(self):
            # unasync: sync: self._thread.join()
            self._done = True
        """
    )
    assert out == "def close(self):\n    self._thread.join()\n    self._done = True\n"


def test_sync_code_is_emitted_verbatim_without_rules() -> None:
    # Rule 11 would turn ``helper_sync`` into ``helper``: the replacement is
    # already sync code and must reach the output exactly as written.
    out = _render_body("def f():\n    return g()  # unasync: sync: return helper_sync()\n")
    assert out == "def f():\n    return helper_sync()\n"


def test_an_empty_sync_replacement_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="sync"):
        unasync.render("x = 1  # unasync: sync:\n", origin=ORIGIN)


# ── Unknown markers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("directive", ["sync-only", "async_only", "bogus", "begin"])
def test_an_unknown_directive_is_an_error_naming_it(directive: str) -> None:
    with pytest.raises(unasync.UnasyncError, match=directive):
        unasync.render(f"x = 1  # unasync: {directive}\n", origin=ORIGIN)


def test_markers_are_consumed() -> None:
    out = unasync.render(
        _src(
            """
            import asyncio  # unasync: sync: import time
            x = 1  # unasync: async-only
            # unasync: async-only-begin
            y = 2
            # unasync: async-only-end
            """
        ),
        origin=ORIGIN,
    )
    assert "unasync:" not in out


# ── Fail-loud on residue ──────────────────────────────────────────────────────


def test_a_residual_await_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="await"):
        unasync.render("x = 1  # unasync: sync: x = await y\n", origin=ORIGIN)


def test_a_residual_async_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="async"):
        unasync.render("# unasync: sync: async def g(): pass\n", origin=ORIGIN)


def test_a_residual_import_asyncio_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="asyncio"):
        unasync.render("import asyncio\n", origin=ORIGIN)


def test_a_residual_from_asyncio_import_is_an_error() -> None:
    with pytest.raises(unasync.UnasyncError, match="asyncio"):
        unasync.render("from asyncio import Lock\n", origin=ORIGIN)


def test_a_marked_asyncio_import_is_not_residue() -> None:
    out = _render_body("import asyncio  # unasync: sync: import threading\n")
    assert out == "import threading\n"


@pytest.mark.parametrize(
    "source",
    [
        "from helpers import AsyncioRunner\n",  # no rule renames Async + lowercase
        "x = 1  # unasync: sync: x = AsyncQuery()\n",  # verbatim sync code
    ],
)
def test_a_residual_async_identifier_is_an_error(source: str) -> None:
    with pytest.raises(unasync.UnasyncError, match="Async"):
        unasync.render(source, origin=ORIGIN)


def test_an_output_that_is_not_python_is_an_error() -> None:
    # The marker drops the opening line of a block: what remains cannot parse.
    source = _src(
        """
        if ready:  # unasync: async-only
            x = 1
        """
    )
    with pytest.raises(unasync.UnasyncError):
        unasync.render(source, origin=ORIGIN)


# ── Header, determinism, idempotence ──────────────────────────────────────────


def test_the_output_opens_with_the_do_not_edit_header() -> None:
    out = unasync.render('"""Doc."""\nx = 1\n', origin=ORIGIN)
    first = out.splitlines()[0]
    assert first.startswith(f"# GENERATED from {ORIGIN} — DO NOT EDIT")


def test_the_header_echoes_the_origin_verbatim() -> None:
    a = unasync.render("x = 1\n", origin="src/niwaki/a.py").splitlines()[0]
    b = unasync.render("x = 1\n", origin="src/niwaki/b.py").splitlines()[0]
    assert "src/niwaki/a.py" in a and "src/niwaki/b.py" in b


def test_the_header_keeps_the_module_docstring_first() -> None:
    out = unasync.render('"""An AsyncQuery module."""\n\nx = 1\n', origin=ORIGIN)
    assert ast.get_docstring(ast.parse(out)) == "An Query module."


_REALISTIC = '''"""Async thing — use ``async for x in AsyncThing()``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Never  # unasync: async-only

from niwaki.transport.session_async import AsyncApicSession


class AsyncThing:
    """Iterate with ``async for``; ``await thing.first()``."""

    def __init__(self, session: AsyncApicSession) -> None:
        self._session = session

    def __aiter__(self) -> AsyncIterator[int]:
        return self.stream()

    # unasync: async-only-begin
    def __iter__(self) -> Never:
        raise TypeError("use async for")
    # unasync: async-only-end

    async def stream(self) -> AsyncIterator[int]:
        async for page in self._session._aiter_pages("p", {}):
            yield len(page)

    async def first(self) -> int | None:
        return [x async for x in self.stream()][0]
'''


def test_render_is_deterministic() -> None:
    assert unasync.render(_REALISTIC, origin=ORIGIN) == unasync.render(_REALISTIC, origin=ORIGIN)


def test_render_is_a_fixed_point_on_its_own_output() -> None:
    once = unasync.render(_REALISTIC, origin=ORIGIN)
    assert _async_nodes(once) == []
    assert unasync.render(_body(once), origin=ORIGIN) == once


@pytest.mark.parametrize(("async_path", "sync_path"), list(unasync.PAIRS))
def test_every_real_async_source_renders(async_path: str, sync_path: str) -> None:
    out = unasync.render((SRC / async_path).read_text(), origin=f"src/niwaki/{async_path}")
    assert _async_nodes(out) == []


def _async_nodes(source: str) -> list[str]:
    """Every async construct left in ``source`` (parsing it on the way)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Await() | ast.AsyncFunctionDef() | ast.AsyncFor() | ast.AsyncWith():
                found.append(type(node).__name__)
            case ast.comprehension(is_async=1):
                found.append("async comprehension")
            case _:
                pass
    return found


# ── main(): --write / --check on a scratch tree ───────────────────────────────


def _scratch_root(tmp_path: Path) -> Path:
    """A package root holding a copy of every real async source of PAIRS."""
    root = tmp_path / "niwaki"
    for async_path, _ in unasync.PAIRS:
        target = root / async_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SRC / async_path, target)
    return root


def test_write_then_check_is_fresh(tmp_path: Path) -> None:
    root = _scratch_root(tmp_path)
    assert unasync.main(["--write", "--root", str(root)]) == 0
    for _, sync_path in unasync.PAIRS:
        assert (root / sync_path).is_file()
    assert unasync.main(["--check", "--root", str(root)]) == 0


def test_a_second_write_changes_no_byte(tmp_path: Path) -> None:
    root = _scratch_root(tmp_path)
    unasync.main(["--write", "--root", str(root)])
    first = {p: (root / p).read_bytes() for _, p in unasync.PAIRS}
    assert unasync.main(["--write", "--root", str(root)]) == 0
    assert {p: (root / p).read_bytes() for _, p in unasync.PAIRS} == first


def test_check_flags_a_hand_edited_target_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scratch_root(tmp_path)
    unasync.main(["--write", "--root", str(root)])
    _, victim = unasync.PAIRS[0]
    tampered = (root / victim).read_text() + "\n# hand edit\n"
    (root / victim).write_text(tampered)
    capsys.readouterr()
    assert unasync.main(["--check", "--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert victim in captured.out + captured.err
    assert (root / victim).read_text() == tampered


def test_check_flags_a_missing_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _scratch_root(tmp_path)
    unasync.main(["--write", "--root", str(root)])
    _, victim = unasync.PAIRS[-1]
    (root / victim).unlink()
    capsys.readouterr()
    assert unasync.main(["--check", "--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert victim in captured.out + captured.err


def test_write_repairs_a_hand_edited_target(tmp_path: Path) -> None:
    root = _scratch_root(tmp_path)
    unasync.main(["--write", "--root", str(root)])
    _, victim = unasync.PAIRS[0]
    pristine = (root / victim).read_bytes()
    (root / victim).write_text("# hand edit\n")
    assert unasync.main(["--write", "--root", str(root)]) == 0
    assert (root / victim).read_bytes() == pristine


def test_a_broken_async_source_fails_the_check(tmp_path: Path) -> None:
    root = _scratch_root(tmp_path)
    unasync.main(["--write", "--root", str(root)])
    async_path, _ = unasync.PAIRS[0]
    source = root / async_path
    source.write_text(source.read_text() + "\nx = 1  # unasync: bogus\n")
    try:
        status = unasync.main(["--check", "--root", str(root)])
    except unasync.UnasyncError:
        return
    assert status != 0


@pytest.mark.parametrize("argv", [[], ["--check", "--write"]])
def test_exactly_one_mode_is_required(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        unasync.main(argv)
    assert excinfo.value.code != 0
