"""Sync/async twin parity guard — a fix applied to one twin must reach the other.

Every sync/async pair in niwaki is hand-maintained twice.  Each divergence the
project has paid for had the same shape: a correction landed in one twin only
(the sync session re-raising a raw ``LoginError`` on a mid-session 401, the
sync socket's ``_ensure_open`` missing its ``_closed`` re-check, the sync
session re-logging in once per thread on a 401 race).  This guard compares
every twin pair function by function, after an AST normalisation that erases
the *expected* sync/async differences, and fails on anything else that is not
an allowlisted, fingerprinted, *intended* divergence.

The normaliser — ``niwaki._codegen.twins`` (API contract)
---------------------------------------------------------

The normaliser and its rule table live in ``src/niwaki/_codegen/twins.py`` so
the future unasync generator shares the exact same table.  The module exposes:

``RULES: tuple[tuple[re.Pattern[str], str], ...]``
    The ordered textual rename table.  Each ``(pattern, replacement)`` pair is
    applied with ``pattern.sub(replacement, text)``, once, in order.  The
    fingerprints in ``tests/twin_parity_allowlist.json`` were computed with
    exactly this table::

        1.  r"\\bStopAsyncIteration\\b"            -> "StopIteration"
        2.  r"\\basynccontextmanager\\b"           -> "contextmanager"
        3.  r"\\b__a(enter|exit|iter|next)__\\b"   -> r"__\\1__"
        4.  r"\\baclose\\b"                        -> "close"
        5.  r"\\basyncio\\.sleep\\b"                -> "time.sleep"
        6.  r"\\b_aiter_"                         -> "_iter_"
        7.  r"\\b_active_session\\b"               -> "_sync_session"
        8.  r"Async(?=[A-Z_])"                   -> ""
        9.  r"_async_"                           -> "_"
        10. r"_async\\b"                          -> ""
        11. r"_sync\\b"                           -> ""

``normalise_name(name: str) -> str``
    Apply ``RULES`` to an identifier or dotted qualified name
    (``"AsyncApicSession._aiter_pages"`` -> ``"ApicSession._iter_pages"``,
    ``"push_async"`` and ``"push_sync"`` -> ``"push"``).

``normalise_code(code: str) -> str``
    Parse ``code`` as a module, apply the AST transform below to the whole
    tree, ``ast.unparse`` it, then apply ``RULES`` to the text.

``normalise(source: str) -> dict[str, str]``
    Parse ``source`` as a module and return one entry per function: every
    ``def``/``async def`` that is a direct statement of the module body or of
    a class body (classes nest: ``Outer.Inner.method``).  Functions nested in
    a function are part of their parent's body, never keys; definitions under
    ``if``/``try`` blocks are not walked.  The key is the **original**
    qualified name (``"AsyncApicSession.login"``, un-normalised — two classes
    of one file normalise to the same name, so the caller pairs them).  The
    value is the function node, transformed, unparsed, then passed through
    ``RULES``.  Several definitions sharing one qualified name
    (``@overload`` stubs, a property getter and setter) are joined with
    ``"\\n"`` in source order.  The dict preserves source order.

The AST transform (applied before unparsing, recursively):

- the docstring — a leading ``Expr`` whose value is a ``str`` constant — is
  removed from every ``FunctionDef``/``AsyncFunctionDef``/``ClassDef``; a body
  left empty becomes ``[ast.Pass()]``;
- ``AsyncFunctionDef`` -> ``FunctionDef`` (same fields, decorators and
  ``type_params`` kept);
- ``Await(value)`` -> ``value``;
- ``AsyncWith`` -> ``With``; ``AsyncFor`` -> ``For``;
- every ``comprehension`` gets ``is_async = 0``.

Comments vanish with the AST.  The output is stable across Python 3.12-3.14
(verified when the fingerprints were recorded).

The guard
---------

``PAIRS`` names each twin pair: the four file pairs (whole modules, minus the
names the async twin imports from the sync one — one definition, not two),
the façade's three class couples, and the design/read shells.  For each pair,
sync and async functions are matched by ``normalise_name`` of their qualified
name (bodies colliding on one normalised name are joined in source order —
``AsyncQuery.__aiter__`` and ``AsyncQuery.__iter__`` both land on
``Query.__iter__``).  A function on one side only is compared against ``""``.

A divergence is the line diff of the two normalised bodies (``-`` sync line,
``+`` async line, from ``difflib.SequenceMatcher`` opcodes, equal runs
dropped); its fingerprint is the SHA-256 of that text.  The allowlist
(``tests/twin_parity_allowlist.json``, section ``parity``) maps
``pair -> normalised name -> {reason, sha256}``.  The guard is red on:

- a divergence with no entry (including a function on one side only);
- an entry whose fingerprint no longer matches (the intended divergence
  moved — review it, then re-record);
- a dead entry (the function is gone, or the twins are now identical);
- an entry without a reason, or naming an unknown pair.

Re-recording is conscious and never adds an entry:
``NIWAKI_TWINS_RECORD=1 uv run pytest tests/test_twin_parity.py`` rewrites
the fingerprints of the *existing* entries that still diverge, then fails so
the rewrite is reviewed in ``git diff``.
"""

from __future__ import annotations

import ast
import difflib
import functools
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pytest

from niwaki._codegen import twins

SRC = Path(__file__).resolve().parents[1] / "src" / "niwaki"
ALLOWLIST_PATH = Path(__file__).with_name("twin_parity_allowlist.json")
RECORD_ENV = "NIWAKI_TWINS_RECORD"


# ── Pair table ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TwinPair:
    """One sync/async twin pair.

    Attributes:
        sync_file: Sync source, relative to ``src/niwaki``.
        async_file: Async source, relative to ``src/niwaki``.
        sync_scope: Top-level names compared on the sync side; ``None`` means
            the whole module (minus the names the async twin imports from it).
        async_scope: Top-level names compared on the async side; ``None``
            means the whole module.
    """

    sync_file: str
    async_file: str
    sync_scope: frozenset[str] | None = None
    async_scope: frozenset[str] | None = None


PAIRS: dict[str, TwinPair] = {
    # The four file pairs: whole modules.
    "session": TwinPair("transport/session.py", "transport/session_async.py"),
    "subscription_socket": TwinPair(
        "transport/_subscription_socket.py", "transport/_subscription_socket_async.py"
    ),
    "query_builder": TwinPair("query/_builder.py", "query/_async_builder.py"),
    "query_subscription": TwinPair("query/_subscription.py", "query/_async_subscription.py"),
    # The façade's three class couples (navigation is already shared).
    "facade_subscription_manager": TwinPair(
        "facade.py",
        "facade.py",
        frozenset({"SubscriptionManager"}),
        frozenset({"AsyncSubscriptionManager"}),
    ),
    "facade_node": TwinPair(
        "facade.py", "facade.py", frozenset({"NiwakiNode"}), frozenset({"AsyncNiwakiNode"})
    ),
    "facade_client": TwinPair(
        "facade.py", "facade.py", frozenset({"Niwaki"}), frozenset({"AsyncNiwaki"})
    ),
    # Design and read: pure cores already shared, thin I/O shells compared.
    "design_push": TwinPair(
        "design/_push.py",
        "design/_push.py",
        frozenset({"push_sync", "_refine_part_sync", "_fetch_bare_sync"}),
        frozenset({"push_async", "_refine_part_async"}),
    ),
    "read": TwinPair(
        "_read.py", "_read.py", frozenset({"read_subtree"}), frozenset({"read_subtree_async"})
    ),
    "design_verify": TwinPair(
        "design/_verify.py",
        "design/_verify.py",
        frozenset({"verify_sync"}),
        frozenset({"verify_async"}),
    ),
    "design_engine": TwinPair(
        "design/_engine.py",
        "design/_engine.py",
        frozenset({"_run_waves_sync"}),
        frozenset({"_run_waves"}),
    ),
}


# ── Comparison machinery ──────────────────────────────────────────────────────


class ParityEntry(TypedDict):
    """One intended divergence: why it exists, and the exact diff it is."""

    reason: str
    sha256: str


def twin_diff(sync_body: str, async_body: str) -> str:
    """Return the line diff of two normalised bodies (``""`` when identical).

    Sync lines are prefixed ``-``, async lines ``+``; equal runs are dropped,
    so the diff — and its fingerprint — only moves when the divergence does.
    """
    a, b = sync_body.splitlines(), async_body.splitlines()
    out: list[str] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.extend("-" + line for line in a[i1:i2])
        out.extend("+" + line for line in b[j1:j2])
    return "\n".join(out)


def fingerprint(diff: str) -> str:
    """SHA-256 of a divergence, as recorded in the allowlist."""
    return hashlib.sha256(diff.encode()).hexdigest()


def shared_names(async_source: str, sync_module: str) -> frozenset[str]:
    """Top-level names the async twin imports from its sync twin.

    Those are defined once and shared, not twinned — the socket's wire types
    (``SubscriptionInfo``, ``SubscriptionGap``…) live in the sync module.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(async_source)):
        if isinstance(node, ast.ImportFrom) and node.module == sync_module:
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _side(source: str, scope: frozenset[str] | None, exclude: frozenset[str]) -> dict[str, str]:
    groups: dict[str, list[str]] = {}
    for qualname, body in twins.normalise(source).items():
        top = qualname.split(".", 1)[0]
        if (scope is not None and top not in scope) or top in exclude:
            continue
        groups.setdefault(twins.normalise_name(qualname), []).append(body)
    return {name: "\n".join(bodies) for name, bodies in groups.items()}


def divergences(
    sync_source: str,
    async_source: str,
    *,
    sync_scope: frozenset[str] | None = None,
    async_scope: frozenset[str] | None = None,
    shared: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Map each normalised function name whose twins differ to its diff."""
    sync_side = _side(sync_source, sync_scope, shared)
    async_side = _side(async_source, async_scope, frozenset())
    found: dict[str, str] = {}
    for name in sorted(sync_side.keys() | async_side.keys()):
        diff = twin_diff(sync_side.get(name, ""), async_side.get(name, ""))
        if diff:
            found[name] = diff
    return found


def _module_name(relative: str) -> str:
    return "niwaki." + relative.removesuffix(".py").replace("/", ".")


def pair_divergences(pair: TwinPair) -> dict[str, str]:
    """Compute the live divergences of one pair from the source tree."""
    sync_source = (SRC / pair.sync_file).read_text()
    async_source = (SRC / pair.async_file).read_text()
    shared = (
        shared_names(async_source, _module_name(pair.sync_file))
        if pair.sync_file != pair.async_file
        else frozenset()
    )
    return divergences(
        sync_source,
        async_source,
        sync_scope=pair.sync_scope,
        async_scope=pair.async_scope,
        shared=shared,
    )


def _kind(diff: str) -> str:
    lines = diff.splitlines()
    if all(line.startswith("-") for line in lines):
        return "sync side only"
    if all(line.startswith("+") for line in lines):
        return "async side only"
    return "bodies differ"


def audit(
    found: Mapping[str, Mapping[str, str]],
    allowlist: Mapping[str, Mapping[str, ParityEntry]],
) -> list[str]:
    """Return every parity problem; an empty list means the twins are in step.

    Args:
        found: ``pair -> normalised name -> diff`` for the pairs audited.
        allowlist: ``pair -> normalised name -> entry`` (intended divergences).
    """
    problems: list[str] = []
    for pair in sorted(allowlist.keys() - found.keys()):
        problems.append(f"{pair}: unknown pair in the allowlist")
    for pair in sorted(found):
        live = found[pair]
        listed = allowlist.get(pair, {})
        for name in sorted(live.keys() | listed.keys()):
            diff = live.get(name)
            entry = listed.get(name)
            if entry is not None and not entry.get("reason", "").strip():
                problems.append(f"{pair}: {name}: allowlist entry without a reason")
            if diff is None:
                problems.append(
                    f"{pair}: {name}: dead allowlist entry — the twins no longer diverge "
                    "here (identical, or the function is gone); delete it"
                )
            elif entry is None:
                problems.append(
                    f"{pair}: {name}: unlisted divergence ({_kind(diff)}) — port the fix "
                    f"to the other twin, or allowlist it with a reason:\n{diff}"
                )
            elif entry["sha256"] != fingerprint(diff):
                problems.append(
                    f"{pair}: {name}: stale fingerprint (allowlisted "
                    f"{entry['sha256'][:12]}, now {fingerprint(diff)[:12]}) — the "
                    "intended divergence changed; review it, and re-record only if the "
                    f"new shape is intended:\n{diff}"
                )
    return problems


@functools.cache
def _allowlist() -> dict[str, dict[str, ParityEntry]]:
    data = json.loads(ALLOWLIST_PATH.read_text())
    parity: dict[str, dict[str, ParityEntry]] = data["parity"]
    return parity


# ── The guard itself ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("pair_name", sorted(PAIRS))
def test_twins_have_not_drifted(pair_name: str) -> None:
    found = {pair_name: pair_divergences(PAIRS[pair_name])}
    listed = {pair_name: _allowlist().get(pair_name, {})}
    problems = audit(found, listed)
    assert not problems, "twin parity broken:\n\n" + "\n\n".join(problems)


def test_the_allowlist_names_only_known_pairs() -> None:
    unknown = sorted(_allowlist().keys() - PAIRS.keys())
    assert not unknown, f"allowlist names unknown pairs: {unknown}"


def test_every_scoped_name_exists() -> None:
    """A renamed shell must not silently empty its pair."""
    for name, pair in PAIRS.items():
        for relative, scope in (
            (pair.sync_file, pair.sync_scope),
            (pair.async_file, pair.async_scope),
        ):
            if scope is None:
                continue
            tree = ast.parse((SRC / relative).read_text())
            defined = {
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            }
            assert scope <= defined, f"{name}: {sorted(scope - defined)} not in {relative}"


def test_same_file_pairs_are_scoped() -> None:
    for name, pair in PAIRS.items():
        if pair.sync_file == pair.async_file:
            assert pair.sync_scope and pair.async_scope, f"{name}: same-file pair needs scopes"


def test_record_fingerprints() -> None:
    """Conscious re-record: only with ``NIWAKI_TWINS_RECORD=1``, never adds entries."""
    if not os.environ.get(RECORD_ENV):
        pytest.skip(f"set {RECORD_ENV}=1 to re-record the existing fingerprints")
    data = json.loads(ALLOWLIST_PATH.read_text())
    rewritten = 0
    for pair_name, entries in data["parity"].items():
        live = pair_divergences(PAIRS[pair_name]) if pair_name in PAIRS else {}
        for name, entry in entries.items():
            if name in live and entry["sha256"] != fingerprint(live[name]):
                entry["sha256"] = fingerprint(live[name])
                rewritten += 1
    ALLOWLIST_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    pytest.fail(f"re-recorded {rewritten} fingerprint(s) — review `git diff {ALLOWLIST_PATH.name}`")


# ── Meta-tests: the guard turns red when it must ──────────────────────────────

_SYNC = '''
class ApicSession:
    def get(self, path):
        """Sync docstring."""
        self._ensure_token()  # a comment
        with self._lock:
            return self._client.get(path)

    def close(self):
        self._client.close()
'''

_ASYNC = '''
class AsyncApicSession:
    async def get(self, path):
        """A different, async docstring."""
        await self._ensure_token()
        async with self._lock:
            return await self._client.get(path)

    async def close(self):
        await self._client.aclose()
'''


class TestGuardMeta:
    def test_identical_twins_are_green(self) -> None:
        found = {"p": divergences(_SYNC, _ASYNC)}
        assert found == {"p": {}}
        assert audit(found, {}) == []

    def test_docstrings_and_comments_are_not_divergences(self) -> None:
        assert divergences(_SYNC.replace("Sync docstring.", "Other words."), _ASYNC) == {}

    def test_a_function_on_the_async_side_only_is_red(self) -> None:
        extra = _ASYNC + "\n    async def _reactive_relogin(self, stale):\n        pass\n"
        found = {"p": divergences(_SYNC, extra)}
        problems = audit(found, {})
        assert len(problems) == 1
        assert "ApicSession._reactive_relogin" in problems[0]
        assert "unlisted divergence" in problems[0]

    def test_a_function_on_the_sync_side_only_is_red(self) -> None:
        extra = _SYNC + "\n    def _open_locked(self):\n        pass\n"
        problems = audit({"p": divergences(extra, _ASYNC)}, {})
        assert len(problems) == 1
        assert "ApicSession._open_locked" in problems[0]

    def test_a_body_divergence_is_red(self) -> None:
        drifted = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(True)")
        problems = audit({"p": divergences(_SYNC, drifted)}, {})
        assert len(problems) == 1
        assert "ApicSession.get" in problems[0]
        assert "bodies differ" in problems[0]

    def test_an_allowlisted_divergence_with_its_exact_fingerprint_is_green(self) -> None:
        drifted = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(True)")
        found = {"p": divergences(_SYNC, drifted)}
        entry: ParityEntry = {
            "reason": "intended",
            "sha256": fingerprint(found["p"]["ApicSession.get"]),
        }
        assert audit(found, {"p": {"ApicSession.get": entry}}) == []

    def test_a_stale_fingerprint_is_red(self) -> None:
        first = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(True)")
        recorded = divergences(_SYNC, first)["ApicSession.get"]
        moved = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(False)")
        entry: ParityEntry = {"reason": "intended", "sha256": fingerprint(recorded)}
        problems = audit({"p": divergences(_SYNC, moved)}, {"p": {"ApicSession.get": entry}})
        assert len(problems) == 1
        assert "stale fingerprint" in problems[0]

    def test_a_dead_entry_for_a_vanished_function_is_red(self) -> None:
        entry: ParityEntry = {"reason": "was intended", "sha256": "0" * 64}
        problems = audit({"p": divergences(_SYNC, _ASYNC)}, {"p": {"ApicSession.gone": entry}})
        assert len(problems) == 1
        assert "dead allowlist entry" in problems[0]

    def test_a_dead_entry_for_now_identical_twins_is_red(self) -> None:
        drifted = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(True)")
        recorded = divergences(_SYNC, drifted)["ApicSession.get"]
        entry: ParityEntry = {"reason": "was intended", "sha256": fingerprint(recorded)}
        problems = audit({"p": divergences(_SYNC, _ASYNC)}, {"p": {"ApicSession.get": entry}})
        assert len(problems) == 1
        assert "dead allowlist entry" in problems[0]

    def test_an_entry_without_a_reason_is_red(self) -> None:
        drifted = _ASYNC.replace("await self._ensure_token()", "await self._ensure_token(True)")
        found = {"p": divergences(_SYNC, drifted)}
        entry: ParityEntry = {"reason": "  ", "sha256": fingerprint(found["p"]["ApicSession.get"])}
        problems = audit(found, {"p": {"ApicSession.get": entry}})
        assert problems == ["p: ApicSession.get: allowlist entry without a reason"]

    def test_an_unknown_pair_is_red(self) -> None:
        entry: ParityEntry = {"reason": "x", "sha256": "0" * 64}
        problems = audit({}, {"ghost": {"f": entry}})
        assert problems == ["ghost: unknown pair in the allowlist"]

    def test_names_imported_from_the_sync_twin_are_shared_not_twinned(self) -> None:
        sync = "class Info:\n    def stale(self):\n        return 1\n\ndef f():\n    pass\n"
        async_ = "from niwaki.x import Info\n\nasync def f():\n    pass\n"
        shared = shared_names(async_, "niwaki.x")
        assert shared == {"Info"}
        assert divergences(sync, async_, shared=shared) == {}
        assert "Info.stale" in divergences(sync, async_)

    def test_colliding_names_on_one_side_are_joined_in_source_order(self) -> None:
        sync = "class Query:\n    def __iter__(self):\n        return self.stream()\n"
        async_ = (
            "class AsyncQuery:\n"
            "    def __aiter__(self):\n        return self.stream()\n"
            "    def __iter__(self):\n        raise TypeError('async')\n"
        )
        diff = divergences(sync, async_)["Query.__iter__"]
        assert diff == "+def __iter__(self):\n+    raise TypeError('async')"


# ── The normaliser contract ───────────────────────────────────────────────────


class TestNormaliser:
    def test_async_constructs_are_erased(self) -> None:
        source = '''
async def fetch(self, path):
    """Docstring."""
    async with self._lock:  # comment
        rows = [r async for r in self.stream()]
    async for page in self._aiter_pages(path):
        await asyncio.sleep(0)
    return await self._client.aclose()
'''
        assert twins.normalise(source) == {
            "fetch": (
                "def fetch(self, path):\n"
                "    with self._lock:\n"
                "        rows = [r for r in self.stream()]\n"
                "    for page in self._iter_pages(path):\n"
                "        time.sleep(0)\n"
                "    return self._client.close()"
            )
        }

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("AsyncApicSession", "ApicSession"),
            ("_AsyncRegistration", "_Registration"),
            ("AsyncIterator", "Iterator"),
            ("StopAsyncIteration", "StopIteration"),
            ("asynccontextmanager", "contextmanager"),
            ("AsyncQuery.__aiter__", "Query.__iter__"),
            ("AsyncQuery.__anext__", "Query.__next__"),
            ("AsyncNiwaki.__aenter__", "Niwaki.__enter__"),
            ("AsyncNiwaki.__aexit__", "Niwaki.__exit__"),
            ("AsyncApicSession._aiter_pages", "ApicSession._iter_pages"),
            ("AsyncNiwaki._active_session", "Niwaki._sync_session"),
            ("read_subtree_async", "read_subtree"),
            ("push_async", "push"),
            ("push_sync", "push"),
            ("_run_waves_sync", "_run_waves"),
            ("_finalize_async_socket", "_finalize_socket"),
            ("niwaki.query._async_builder", "niwaki.query._builder"),
            ("niwaki.transport.session_async", "niwaki.transport.session"),
            ("Niwaki._sync_session", "Niwaki._sync_session"),
            ("ApicSession.get", "ApicSession.get"),
        ],
    )
    def test_normalise_name(self, name: str, expected: str) -> None:
        assert twins.normalise_name(name) == expected

    def test_rules_is_the_table_normalise_name_applies(self) -> None:
        """The table is the single source of truth, shared with unasync."""
        assert twins.RULES, "the rule table must not be empty"
        for pattern, replacement in twins.RULES:
            assert isinstance(pattern, re.Pattern)
            assert isinstance(replacement, str)
        for name in ("AsyncApicSession.__aenter__", "_finalize_async_socket", "push_sync"):
            applied = name
            for pattern, replacement in twins.RULES:
                applied = pattern.sub(replacement, applied)
            assert twins.normalise_name(name) == applied

    def test_keys_are_original_qualified_names_in_source_order(self) -> None:
        source = (
            "def top():\n    def inner():\n        pass\n    return inner\n\n"
            "class AsyncOuter:\n"
            "    class Inner:\n        async def m(self):\n            pass\n"
            "    async def n(self):\n        pass\n"
        )
        result = twins.normalise(source)
        assert list(result) == ["top", "AsyncOuter.Inner.m", "AsyncOuter.n"]
        assert "def inner():" in result["top"]

    def test_definitions_sharing_a_qualified_name_are_joined(self) -> None:
        source = (
            "class C:\n"
            "    @overload\n    def q(self, x: int) -> int: ...\n"
            "    @overload\n    def q(self, x: str) -> str: ...\n"
            "    def q(self, x):\n        return x\n"
        )
        assert twins.normalise(source) == {
            "C.q": (
                "@overload\ndef q(self, x: int) -> int:\n    ...\n"
                "@overload\ndef q(self, x: str) -> str:\n    ...\n"
                "def q(self, x):\n    return x"
            )
        }

    def test_a_docstring_only_body_becomes_pass(self) -> None:
        assert twins.normalise('async def f():\n    """Only a docstring."""\n') == {
            "f": "def f():\n    pass"
        }

    def test_normalise_code_erases_a_whole_module(self) -> None:
        source = (
            "import asyncio\n\n"
            'class AsyncS:\n    """Doc."""\n\n'
            "    async def __aenter__(self) -> AsyncS:\n"
            "        await self.login()\n        return self\n"
        )
        assert twins.normalise_code(source) == (
            "import asyncio\n\n"
            "class S:\n\n"
            "    def __enter__(self) -> S:\n"
            "        self.login()\n        return self"
        )


_TWIN_FILES = sorted(
    {pair.sync_file for pair in PAIRS.values()} | {pair.async_file for pair in PAIRS.values()}
)


@pytest.mark.parametrize("relative", _TWIN_FILES)
def test_normalisation_is_idempotent(relative: str) -> None:
    source = (SRC / relative).read_text()
    once = twins.normalise_code(source)
    assert twins.normalise_code(once) == once
    grouped: dict[str, list[str]] = {}
    for qualname, body in twins.normalise(source).items():
        grouped.setdefault(twins.normalise_name(qualname), []).append(body)
    assert twins.normalise(once) == {name: "\n".join(b) for name, b in grouped.items()}
