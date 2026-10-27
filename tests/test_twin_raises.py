"""Twin ``Raises:`` contract — both twins of a public method promise the same errors.

The AST parity guard (``tests/test_twin_parity.py``) ignores docstrings by
design.  But the docstrings are the public contract (autodoc renders them), and
they drift exactly like the code does: a twin documents an exception its
sibling forgets.  This guard reads the Google-style ``Raises:`` section of
every public method of each twin class couple and requires the two sides to
declare the same set of exceptions.

What is compared
----------------

For each couple in ``RAISES_PAIRS`` — the sessions, the query builders, the
subscriptions (query-level, raw, socket) and the façade's three couples — the
methods defined in each class body whose name is public (no leading
underscore) or a dunder, plus the class docstring itself (key ``<class>``,
which documents ``__init__``).  Methods are matched by
``niwaki._codegen.twins.normalise_name`` (``__aenter__`` meets ``__enter__``);
a method present on one side only is the parity guard's business, not this
one's.  Two methods of one class landing on the same normalised name
(``AsyncQuery.__aiter__`` and ``AsyncQuery.__iter__``) pool their sets.

How a ``Raises:`` section is read
---------------------------------

The section starts at a line that is exactly ``Raises:`` (any indentation) and
ends at the next section header (``Args:``, ``Returns:``, ``Example::``…) or
at the first non-blank line indented no deeper than the header.  Entries are
the lines at the first indentation level under the header; deeper lines are
an entry's continuation.  An entry ``Name: text`` (``:exc:`~pkg.Name```,
backticks and a dotted path accepted) contributes ``Name`` passed through
``normalise_name`` (so ``StopAsyncIteration`` ≡ ``StopIteration``).  An entry
``See :meth:`get`.`` contributes ``see:get``.  Anything else contributes
``text:<the line>``.  No docstring or no section is the empty set.

Intended gaps live in ``tests/twin_parity_allowlist.json`` under ``raises``:
``couple -> normalised method -> {reason, sync, async}`` with both sets pinned,
so an entry goes stale the moment either side's section moves, and dead when
the two sides agree again.
"""

from __future__ import annotations

import ast
import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TypedDict

import pytest

from niwaki._codegen import twins

SRC = Path(__file__).resolve().parents[1] / "src" / "niwaki"
ALLOWLIST_PATH = Path(__file__).with_name("twin_parity_allowlist.json")


@dataclass(frozen=True, slots=True)
class ClassCouple:
    """A sync class and its async twin, each in its module (relative to ``src/niwaki``)."""

    sync_file: str
    sync_class: str
    async_file: str
    async_class: str


_SOCKET = "transport/_subscription_socket.py"
_SOCKET_ASYNC = "transport/_subscription_socket_async.py"

RAISES_PAIRS: dict[str, ClassCouple] = {
    "session": ClassCouple(
        "transport/session.py", "ApicSession", "transport/session_async.py", "AsyncApicSession"
    ),
    "query_builder": ClassCouple(
        "query/_builder.py", "Query", "query/_async_builder.py", "AsyncQuery"
    ),
    "query_subscription": ClassCouple(
        "query/_subscription.py",
        "Subscription",
        "query/_async_subscription.py",
        "AsyncSubscription",
    ),
    "raw_subscription": ClassCouple(
        _SOCKET, "RawSubscription", _SOCKET_ASYNC, "AsyncRawSubscription"
    ),
    "subscription_socket": ClassCouple(
        _SOCKET, "SubscriptionSocket", _SOCKET_ASYNC, "AsyncSubscriptionSocket"
    ),
    "facade_client": ClassCouple("facade.py", "Niwaki", "facade.py", "AsyncNiwaki"),
    "facade_node": ClassCouple("facade.py", "NiwakiNode", "facade.py", "AsyncNiwakiNode"),
    "facade_subscription_manager": ClassCouple(
        "facade.py", "SubscriptionManager", "facade.py", "AsyncSubscriptionManager"
    ),
}


# One intended gap, both declared sets pinned ("async" is a keyword, hence the
# functional form).
RaisesEntry = TypedDict("RaisesEntry", {"reason": str, "sync": list[str], "async": list[str]})


# ── Parsing ───────────────────────────────────────────────────────────────────

_HEADER = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Z][A-Za-z ]*):(?::)?\s*$")
_ENTRY = re.compile(r"^(?::exc:)?`*~?(?P<name>[A-Za-z_][\w.]*)`*\s*:")
_SEE = re.compile(r"^See\s+:(?:meth|func|attr):`~?\.?(?P<target>[\w.]+)`")


def raises_of(docstring: str | None) -> frozenset[str]:
    """Return the exception tokens declared in a docstring's ``Raises:`` section."""
    if not docstring:
        return frozenset()
    found: set[str] = set()
    inside = False
    header_indent = 0
    entry_indent: int | None = None
    for line in docstring.expandtabs().splitlines():
        header = _HEADER.match(line)
        if header:
            inside = header["name"] == "Raises"
            header_indent = len(header["indent"])
            entry_indent = None
            continue
        if not inside or not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            inside = False
            continue
        if entry_indent is None:
            entry_indent = indent
        if indent != entry_indent:
            continue  # continuation of the previous entry
        text = line.strip()
        if see := _SEE.match(text):
            found.add("see:" + see["target"].rsplit(".", 1)[-1])
        elif entry := _ENTRY.match(text):
            found.add(twins.normalise_name(entry["name"].rsplit(".", 1)[-1]))
        else:
            found.add("text:" + text)
    return frozenset(found)


def _is_public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def class_contract(source: str, class_name: str) -> dict[str, frozenset[str]]:
    """Map ``<class>`` and each public method (normalised) to its declared set."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            contract: dict[str, frozenset[str]] = {"<class>": raises_of(ast.get_docstring(node))}
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(
                    item.name
                ):
                    key = twins.normalise_name(item.name)
                    declared = raises_of(ast.get_docstring(item))
                    contract[key] = contract.get(key, frozenset()) | declared
            return contract
    raise LookupError(f"class {class_name} not found")


def contract_gaps(
    sync: Mapping[str, frozenset[str]], async_: Mapping[str, frozenset[str]]
) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Methods present on both sides whose declared sets differ."""
    return {
        name: (sync[name], async_[name])
        for name in sorted(sync.keys() & async_.keys())
        if sync[name] != async_[name]
    }


def audit_raises(
    found: Mapping[str, Mapping[str, tuple[frozenset[str], frozenset[str]]]],
    allowlist: Mapping[str, Mapping[str, RaisesEntry]],
) -> list[str]:
    """Return every contract problem; empty means the twins promise the same errors."""
    problems = [
        f"{c}: unknown couple in the allowlist" for c in sorted(allowlist.keys() - found.keys())
    ]
    for couple in sorted(found):
        live = found[couple]
        listed = allowlist.get(couple, {})
        for name in sorted(live.keys() | listed.keys()):
            entry = listed.get(name)
            gap = live.get(name)
            if entry is not None and not entry.get("reason", "").strip():
                problems.append(f"{couple}: {name}: allowlist entry without a reason")
            if gap is None:
                problems.append(
                    f"{couple}: {name}: dead allowlist entry — the twins' Raises: sections now "
                    "agree (or the method is gone); delete it"
                )
                continue
            sync_set, async_set = gap
            detail = (
                f"sync only: {sorted(sync_set - async_set)}, "
                f"async only: {sorted(async_set - sync_set)}"
            )
            if entry is None:
                problems.append(
                    f"{couple}: {name}: Raises: sections differ ({detail}) — align the "
                    "docstrings, or allowlist the gap with a reason"
                )
            elif frozenset(entry["sync"]) != sync_set or frozenset(entry["async"]) != async_set:
                problems.append(
                    f"{couple}: {name}: stale allowlist entry — the declared sets moved ({detail})"
                )
    return problems


@functools.cache
def _allowlist() -> dict[str, dict[str, RaisesEntry]]:
    data = json.loads(ALLOWLIST_PATH.read_text())
    raises: dict[str, dict[str, RaisesEntry]] = data["raises"]
    return raises


def couple_gaps(couple: ClassCouple) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    sync = class_contract((SRC / couple.sync_file).read_text(), couple.sync_class)
    async_ = class_contract((SRC / couple.async_file).read_text(), couple.async_class)
    return contract_gaps(sync, async_)


# ── The guard ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("couple_name", sorted(RAISES_PAIRS))
def test_twins_declare_the_same_exceptions(couple_name: str) -> None:
    found = {couple_name: couple_gaps(RAISES_PAIRS[couple_name])}
    problems = audit_raises(found, {couple_name: _allowlist().get(couple_name, {})})
    assert not problems, "Raises: contract broken:\n" + "\n".join(problems)


def test_the_allowlist_names_only_known_couples() -> None:
    unknown = sorted(_allowlist().keys() - RAISES_PAIRS.keys())
    assert not unknown, f"raises allowlist names unknown couples: {unknown}"


def test_every_couple_has_public_methods_to_compare() -> None:
    for name, couple in RAISES_PAIRS.items():
        sync = class_contract((SRC / couple.sync_file).read_text(), couple.sync_class)
        async_ = class_contract((SRC / couple.async_file).read_text(), couple.async_class)
        assert len(sync.keys() & async_.keys()) > 1, f"{name}: nothing compared"


# ── Meta-tests ────────────────────────────────────────────────────────────────


class TestRaisesParser:
    def test_reads_names_and_ignores_continuations_and_other_sections(self) -> None:
        doc = """Fetch.

        Args:
            path: Not an exception: really.

        Raises:
            NotFoundError: HTTP 404 — the MO
                does not exist. Continuation: ignored.
            :exc:`~niwaki.exceptions.ForbiddenError`: HTTP 403.
            ``TimeoutError``: too slow.
            StopAsyncIteration: end of stream.

        Returns:
            Things: also not an exception.
        """
        assert raises_of(doc) == {
            "NotFoundError",
            "ForbiddenError",
            "TimeoutError",
            "StopIteration",
        }

    def test_a_see_reference_is_a_token(self) -> None:
        doc = "Write.\n\nRaises:\n    See :meth:`post_mo`.\n"
        assert raises_of(doc) == {"see:post_mo"}

    def test_free_text_is_kept_verbatim(self) -> None:
        assert raises_of("X.\n\nRaises:\n    Nothing in practice.\n") == {
            "text:Nothing in practice."
        }

    def test_no_docstring_and_no_section_are_empty(self) -> None:
        assert raises_of(None) == frozenset()
        assert raises_of("Just a summary.") == frozenset()

    def test_twin_dunders_and_pooled_collisions(self) -> None:
        source = (
            "class AsyncQ:\n"
            '    async def __aenter__(self):\n        """E.\n\n        Raises:\n'
            '            LoginError: no.\n        """\n'
            '    def __aiter__(self):\n        """I."""\n'
            '    def __iter__(self):\n        """J.\n\n        Raises:\n'
            '            TypeError: always.\n        """\n'
            '    def _private(self):\n        """P.\n\n        Raises:\n'
            '            KeyError: hidden.\n        """\n'
        )
        assert class_contract(source, "AsyncQ") == {
            "<class>": frozenset(),
            "__enter__": frozenset({"LoginError"}),
            "__iter__": frozenset({"TypeError"}),
        }


class TestRaisesGuardMeta:
    _SYNC: ClassVar[dict[str, frozenset[str]]] = {
        "get": frozenset({"NotFoundError"}),
        "close": frozenset(),
    }

    def test_equal_contracts_are_green(self) -> None:
        found = {"c": contract_gaps(self._SYNC, dict(self._SYNC))}
        assert audit_raises(found, {}) == []

    def test_a_missing_exception_is_red(self) -> None:
        async_ = {"get": frozenset({"NotFoundError", "TimeoutError"}), "close": frozenset()}
        problems = audit_raises({"c": contract_gaps(self._SYNC, async_)}, {})
        assert len(problems) == 1
        assert "c: get: Raises: sections differ" in problems[0]
        assert "async only: ['TimeoutError']" in problems[0]

    def test_a_pinned_intended_gap_is_green(self) -> None:
        async_ = {"get": frozenset({"NotFoundError", "TypeError"}), "close": frozenset()}
        entry: RaisesEntry = {
            "reason": "async-only trap",
            "sync": ["NotFoundError"],
            "async": ["NotFoundError", "TypeError"],
        }
        assert audit_raises({"c": contract_gaps(self._SYNC, async_)}, {"c": {"get": entry}}) == []

    def test_a_moved_gap_is_stale(self) -> None:
        async_ = {"get": frozenset({"NotFoundError", "ValueError"}), "close": frozenset()}
        entry: RaisesEntry = {
            "reason": "async-only trap",
            "sync": ["NotFoundError"],
            "async": ["NotFoundError", "TypeError"],
        }
        problems = audit_raises({"c": contract_gaps(self._SYNC, async_)}, {"c": {"get": entry}})
        assert len(problems) == 1
        assert "stale allowlist entry" in problems[0]

    def test_a_closed_gap_leaves_a_dead_entry(self) -> None:
        entry: RaisesEntry = {"reason": "was intended", "sync": [], "async": ["TypeError"]}
        problems = audit_raises(
            {"c": contract_gaps(self._SYNC, dict(self._SYNC))}, {"c": {"get": entry}}
        )
        assert len(problems) == 1
        assert "dead allowlist entry" in problems[0]
