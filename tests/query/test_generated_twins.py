"""The generated sync query twins: migration proof, allowlist, docstrings.

``query/_builder.py`` and ``query/_subscription.py`` stop being hand-written:
they are generated from ``query/_async_builder.py`` and
``query/_async_subscription.py`` by ``niwaki._codegen.unasync`` (engine
contract: ``tests/test_unasync.py``; byte freshness:
``tests/test_unasync_fresh.py``).  This module pins three things.

1. **The one-shot migration proof.**  The sync twins as they were last written
   by hand are frozen in ``tests/fixtures/unasync/handwritten_*.py.txt``.
   Rendering today's async sources must give the same program, compared after
   the parity guard's own normalisation (``niwaki._codegen.twins.normalise``:
   docstrings and comments erased) — function by function, plus the module
   skeleton (imported names, module-level statements, class headers) — modulo
   the intended differences listed in :data:`INTENDED`, and nothing else.
   This is what makes the switch safe: the generated code *is* the code the
   whole query suite validated.  It is one-shot by nature: when a later change
   deliberately moves a function of the async query sources, update (or
   retire) the frozen fixture in that same change — from then on the parity
   guard and the equivalence matrices are the permanent net.

2. **The allowlist decision** for the two query pairs in
   ``tests/twin_parity_allowlist.json``.  Once the sync side is generated,
   every divergence it has from its async source is *produced by a marker*.
   The marker language is kept minimal on purpose (three directives, used only
   for real semantic differences), so:

   - ``query_builder / Query.__iter__`` **stays**, reason and fingerprint
     unchanged: ``AsyncQuery.__iter__`` raising ``TypeError`` is a real
     async-only behaviour (the ``async-only-begin/-end`` block), and the
     generated ``__iter__`` (from ``__aiter__``) normalises exactly like the
     hand-written one did.  Its ``raises`` entry stays too;
   - ``query_builder / Query.fetch`` is **retired**: ``list(self.stream())``
     versus the async comprehension was cosmetic; the generated twin is the
     plain comprehension, identical to its source once normalised;
   - ``query_subscription / Subscription.__next__`` is **retired**:
     ``next(self._raw)`` versus ``await self._raw.__anext__()`` was cosmetic;
     the generated twin calls ``self._raw.__next__()``.

   Keeping either cosmetic shape alive with a ``sync:`` marker would spend the
   escape hatch on style — exactly the drift into a marker language the
   design rules out.  The parity guard confirms the decision from the other
   side: a retired entry left in the allowlist would be reported dead.

3. **The public docstrings stay meaningful (decision D1).**  The sync
   docstrings are now derived from the async ones (rule-renamed, ``await`` /
   ``async for`` / ``async with`` dropped in examples), the async text being
   rewritten so that both renderings read right, merged toward the richer of
   the two former texts.  For ``Query`` and ``Subscription``, the class and
   every public method must speak sync: no ``await``, no ``async`` construct,
   no ``Async*`` name, no coroutine, no "sync twin" pointer, no reference to
   itself; the class docstrings show ``for … in`` / ``with`` usage; and no
   section or distinctive fact of the former hand-written sync text is lost.
   Checked on the committed file and on a fresh rendering of the async source.

Behaviour is not re-tested here: the existing query suites
(``tests/query/test_builder.py``, ``test_subscription.py``, their async
siblings…) and the equivalence matrix ``tests/query/test_builder_twins.py``
run unchanged against the generated twins and are the behavioural net.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Literal

import pytest

from niwaki._codegen import twins

TESTS = Path(__file__).resolve().parents[1]
SRC = TESTS.parent / "src" / "niwaki"
FIXTURES = TESTS / "fixtures" / "unasync"
ALLOWLIST = TESTS / "twin_parity_allowlist.json"

#: (async source, committed sync target, frozen hand-written sync) per pair.
PAIRS: dict[str, tuple[str, str, str]] = {
    "query_builder": (
        "query/_async_builder.py",
        "query/_builder.py",
        "handwritten_builder.py.txt",
    ),
    "query_subscription": (
        "query/_async_subscription.py",
        "query/_subscription.py",
        "handwritten_subscription.py.txt",
    ),
}

#: The intended differences between the generated and the hand-written twin,
#: in normalised form: ``qualified name -> (hand-written fragment, generated
#: fragment)``.  Everything else must be identical.
INTENDED: dict[str, dict[str, tuple[str, str]]] = {
    "query_builder": {
        "Query.fetch": (
            "return list(self.stream())",
            "return [obj for obj in self.stream()]",
        ),
    },
    "query_subscription": {
        "Subscription.__next__": (
            "event_from_raw(next(self._raw))",
            "event_from_raw(self._raw.__next__())",
        ),
    },
}


def _rendered(pair: str) -> str:
    # Imported here, not at module level, so the checks that do not need the
    # generator (the allowlist, the committed docstrings) report on their own.
    from niwaki._codegen import unasync

    async_path, _, _ = PAIRS[pair]
    source = (SRC / async_path).read_text(encoding="utf-8")
    return unasync.render(source, origin=f"src/niwaki/{async_path}")


def _handwritten(pair: str) -> str:
    return (FIXTURES / PAIRS[pair][2]).read_text(encoding="utf-8")


def _committed(pair: str) -> str:
    return (SRC / PAIRS[pair][1]).read_text(encoding="utf-8")


# ── 1. The one-shot migration proof ───────────────────────────────────────────


def _imports(body: list[ast.stmt]) -> set[tuple[str, str, str | None]]:
    """Module-level imported names, ``if TYPE_CHECKING:`` blocks included."""
    found: set[tuple[str, str, str | None]] = set()
    for statement in body:
        match statement:
            case ast.Import(names=names):
                found.update(("", alias.name, alias.asname) for alias in names)
            case ast.ImportFrom(module=module, names=names, level=level):
                found.update(
                    ("." * level + (module or ""), alias.name, alias.asname) for alias in names
                )
            case ast.If(body=inner, orelse=orelse):
                found |= _imports(inner) | _imports(orelse)
            case _:
                pass
    return found


def _skeleton(source: str) -> dict[str, object]:
    """Everything of a module but its function bodies, docstrings and comments.

    Imports become a set of ``(module, name, alias)`` (how they are grouped on
    lines is ruff's business); classes keep their header — bases, keywords,
    decorators, type parameters — and their non-function statements; other
    module-level statements are unparsed, then normalised like the functions.
    """
    tree = ast.parse(source)
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the module docstring
    classes: list[str] = []
    statements: list[str] = []
    for statement in body:
        match statement:
            case ast.Import() | ast.ImportFrom():
                pass
            case ast.If(test=ast.Name(id="TYPE_CHECKING")):
                pass
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                pass
            case ast.ClassDef():
                header = ast.ClassDef(
                    name=statement.name,
                    bases=statement.bases,
                    keywords=statement.keywords,
                    body=[
                        member
                        for member in statement.body
                        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                        and not (
                            isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant)
                        )
                    ]
                    or [ast.Pass()],
                    decorator_list=statement.decorator_list,
                    type_params=statement.type_params,
                )
                classes.append(twins.normalise_code(ast.unparse(header)))
            case _:
                statements.append(twins.normalise_code(ast.unparse(statement)))
    return {"imports": _imports(body), "classes": classes, "statements": statements}


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_the_generated_twin_has_the_hand_written_functions(pair: str) -> None:
    generated = twins.normalise(_rendered(pair))
    handwritten = twins.normalise(_handwritten(pair))
    assert list(generated) == list(handwritten), "functions added, removed or reordered"
    intended = INTENDED[pair]
    for name, body in handwritten.items():
        expected = body
        if name in intended:
            before, after = intended[name]
            assert before in body, f"{name}: the frozen hand-written shape moved"
            expected = body.replace(before, after)
        assert generated[name] == expected, f"{name}: the generated twin changed behaviour"


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_the_generated_twin_has_the_hand_written_skeleton(pair: str) -> None:
    assert _skeleton(_rendered(pair)) == _skeleton(_handwritten(pair))


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_the_frozen_fixture_was_the_hand_written_twin(pair: str) -> None:
    """The fixture is a faithful freeze: it pins no generated header."""
    assert "GENERATED" not in _handwritten(pair).splitlines()[0]
    ast.parse(_handwritten(pair))


# ── 2. The allowlist decision ─────────────────────────────────────────────────


def _allowlist() -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    data: dict[str, dict[str, dict[str, dict[str, object]]]] = json.loads(
        ALLOWLIST.read_text(encoding="utf-8")
    )
    return data


ITER_TRAP_REASON = (
    "Async-only trap: AsyncQuery.__iter__ raises TypeError so `for x in async_query` "
    "fails loudly instead of iterating a coroutine; AsyncQuery.__aiter__ carries the "
    "real iteration."
)
ITER_TRAP_SHA256 = "6b6b982e8c15ed6e0bd5a3216f8d4a47cb1f2e42c5c555307a22d091bb20e759"


def test_the_async_only_iteration_trap_stays_allowlisted_unchanged() -> None:
    entry = _allowlist()["parity"]["query_builder"]["Query.__iter__"]
    assert entry == {"reason": ITER_TRAP_REASON, "sha256": ITER_TRAP_SHA256}


def test_the_async_only_iteration_trap_keeps_its_raises_entry() -> None:
    entry = _allowlist()["raises"]["query_builder"]["__iter__"]
    assert entry["sync"] == [] and entry["async"] == ["TypeError"]


def test_the_cosmetic_query_divergences_are_retired() -> None:
    parity = _allowlist()["parity"]
    assert set(parity["query_builder"]) == {"Query.__iter__"}
    assert parity.get("query_subscription", {}) == {}


# ── 3. The public docstrings stay meaningful (D1) ─────────────────────────────

CLASSES: dict[str, str] = {"query_builder": "Query", "query_subscription": "Subscription"}

#: Async vocabulary that must never reach a sync docstring.
FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("await", re.compile(r"\bawait(?:s|ed|ing)?\b", re.IGNORECASE)),
    ("async", re.compile(r"\basync\b", re.IGNORECASE)),
    ("asyncio", re.compile(r"\basyncio\b")),
    ("an Async* name", re.compile(r"\bAsync[A-Z_]\w*")),
    ("asynchronous", re.compile(r"\basynchronous(?:ly)?\b", re.IGNORECASE)),
    ("coroutine", re.compile(r"\bcoroutines?\b", re.IGNORECASE)),
    ("a twin pointer", re.compile(r"\bsync twin\b", re.IGNORECASE)),
)

#: Facts only the former hand-written sync text carried: D1 merges toward
#: the richer text, so they must survive the derivation.
RICHER_FACTS: dict[str, tuple[str, ...]] = {
    "Query.fetch": ("[:n]",),
    "Query.first": ("is None",),
    "Query.count": ("count-only",),
    "Query.subscribe": ("page_size()",),
    "Query.execute_raw": ("moCount", "Example::"),
    "Subscription.refresh_now": ("failure counter",),
}

SECTION = re.compile(r"^\s*(Args|Returns|Yields|Raises|Attributes|Example|Examples)::?\s*$")


def _public_docstrings(source: str, cls: str) -> dict[str, str]:
    """``{"<cls>": class doc, "<cls>.<method>": doc}`` for the public surface.

    Public means a name without a leading underscore, or a dunder; methods
    and properties without a docstring are skipped.
    """
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    docs = {cls: ast.get_docstring(node) or ""}
    for member in node.body:
        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        name = member.name
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            continue
        doc = ast.get_docstring(member)
        if doc:
            docs[f"{cls}.{name}"] = doc
    return docs


type Flavour = Literal["committed", "rendered"]


def _sync_source(pair: str, flavour: Flavour) -> str:
    return _committed(pair) if flavour == "committed" else _rendered(pair)


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
@pytest.mark.parametrize("pair", sorted(CLASSES))
def test_sync_docstrings_carry_no_async_vocabulary(pair: str, flavour: Flavour) -> None:
    docs = _public_docstrings(_sync_source(pair, flavour), CLASSES[pair])
    problems = [
        f"{where}: {label} — …{doc[max(0, hit.start() - 30) : hit.end() + 30]!r}…"
        for where, doc in docs.items()
        for label, pattern in FORBIDDEN
        if (hit := pattern.search(doc))
    ]
    assert not problems, "async vocabulary in a sync docstring:\n" + "\n".join(problems)


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
@pytest.mark.parametrize("pair", sorted(CLASSES))
def test_sync_docstrings_never_point_at_themselves(pair: str, flavour: Flavour) -> None:
    """A derived ``See :meth:`Twin.method``` must not land on its own method."""
    docs = _public_docstrings(_sync_source(pair, flavour), CLASSES[pair])
    problems = [
        where
        for where, doc in docs.items()
        if "." in where
        and re.search(r":(?:meth|attr|func):`~?[\w.]*\b" + re.escape(where) + r"`", doc)
    ]
    assert not problems, f"self-referencing docstrings: {problems}"


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
def test_the_query_docstrings_show_sync_usage(flavour: Flavour) -> None:
    docs = _public_docstrings(_sync_source("query_builder", flavour), "Query")
    assert re.search(r"^\s*for \w+ in \S.*:$", docs["Query"], re.MULTILINE), docs["Query"]
    assert re.search(r"^\s*with \S.* as \w+:$", docs["Query"], re.MULTILINE), docs["Query"]
    assert re.search(r"\bfor \w+ in (query|q)\b", docs["Query.__iter__"]), docs["Query.__iter__"]


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
def test_the_subscription_docstrings_show_sync_usage(flavour: Flavour) -> None:
    doc = _public_docstrings(_sync_source("query_subscription", flavour), "Subscription")
    text = doc["Subscription"]
    assert re.search(r"^\s*for \w+ in sub:$", text, re.MULTILINE), text
    assert re.search(r"^\s*with \S.* as sub:$", text, re.MULTILINE), text
    assert "StopIteration" in doc["Subscription.__next__"]


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
@pytest.mark.parametrize("pair", sorted(CLASSES))
def test_no_section_of_the_former_sync_docstrings_is_lost(pair: str, flavour: Flavour) -> None:
    cls = CLASSES[pair]
    before = _public_docstrings(_handwritten(pair), cls)
    after = _public_docstrings(_sync_source(pair, flavour), cls)
    for where, doc in before.items():
        sections = {m.group(1) for line in doc.splitlines() if (m := SECTION.match(line))}
        kept = {
            m.group(1) for line in after.get(where, "").splitlines() if (m := SECTION.match(line))
        }
        assert sections <= kept, f"{where}: lost {sorted(sections - kept)}"


@pytest.mark.parametrize("flavour", ["committed", "rendered"])
@pytest.mark.parametrize(("where", "facts"), sorted(RICHER_FACTS.items()))
def test_the_richer_former_sync_text_survives(
    where: str, facts: tuple[str, ...], flavour: Flavour
) -> None:
    pair = "query_builder" if where.startswith("Query.") else "query_subscription"
    cls = CLASSES[pair]
    doc = _public_docstrings(_sync_source(pair, flavour), cls).get(where, "")
    missing = [fact for fact in facts if fact not in doc]
    assert not missing, f"{where}: the derived docstring lost {missing}"
