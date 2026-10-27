"""Sync/async twin normaliser — erase the *expected* differences between twins.

Every sync/async pair in niwaki (the sessions, the subscription sockets, the
query builders and subscriptions, the façade couples, the design and read
shells) is maintained twice.  A fix that lands in one twin only is the
recurring defect of such a layout, so the parity guard
(``tests/test_twin_parity.py``) compares the two sides function by function.
To make that comparison meaningful, both sides are first brought to a common
**normal form** in which everything that *must* differ between a sync and an
async implementation has been erased:

1. **An AST transform** removes the syntactic async markers — ``async def``,
   ``await``, ``async with``, ``async for`` and async comprehensions — and
   strips docstrings (documentation is compared separately, comments vanish
   with the AST).
2. **A textual rename table** (:data:`RULES`) maps the async vocabulary onto
   the sync one: ``AsyncApicSession`` → ``ApicSession``, ``__aenter__`` →
   ``__enter__``, ``aclose`` → ``close``, ``asyncio.sleep`` → ``time.sleep``,
   ``push_async`` / ``push_sync`` → ``push``…

Whatever survives both steps is a genuine divergence: either an intended one
(recorded, with a reason, in ``tests/twin_parity_allowlist.json``) or a fix
that reached only one twin.

The rule table is deliberately a module-level constant: the parity guard and
any future sync-from-async generator must share *exactly* the same table, or
a generated twin would be compared under different rules than it was made
with.  The fingerprints in the allowlist are SHA-256 digests of the output of
this module, so any change to the table or the transform is a conscious
re-record (``NIWAKI_TWINS_RECORD=1``).

This module is a pure, corpus-free text-and-AST utility: it reads no schema
and emits no artifact, which is why the regen freshness manifest does not
track it (see :mod:`niwaki._codegen.freshness`).
"""

from __future__ import annotations

import ast
import re
from typing import Final, override

__all__ = ["RULES", "normalise", "normalise_code", "normalise_name"]


def _compile_rules(
    table: tuple[tuple[str, str], ...],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile a ``(pattern, replacement)`` source table, preserving its order."""
    return tuple((re.compile(pattern), replacement) for pattern, replacement in table)


RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = _compile_rules(
    (
        # Builtins and stdlib names whose async spelling differs.
        (r"\bStopAsyncIteration\b", "StopIteration"),
        (r"\basynccontextmanager\b", "contextmanager"),
        # Async dunders: __aenter__/__aexit__/__aiter__/__anext__.
        (r"\b__a(enter|exit|iter|next)__\b", r"__\1__"),
        (r"\baclose\b", "close"),
        (r"\basyncio\.sleep\b", "time.sleep"),
        # Private async iterators (``_aiter_pages`` → ``_iter_pages``).
        (r"\b_aiter_", "_iter_"),
        # The façade's session accessor carries a flavour-specific name.
        (r"\b_active_session\b", "_sync_session"),
        # Class-name prefix: AsyncApicSession, _AsyncRegistration, AsyncIterator.
        (r"Async(?=[A-Z_])", ""),
        # Module and function affixes: _async_builder, push_async, push_sync.
        (r"_async_", "_"),
        (r"_async\b", ""),
        (r"_sync\b", ""),
    )
)
"""The ordered textual rename table, shared by every twin tool.

Each ``(pattern, replacement)`` pair is applied once, in order, with
``pattern.sub(replacement, text)``.  Order matters: the dunder rule must run
before the generic ``Async`` prefix rule, and ``_async_`` before ``_async``.

The table works on any text — identifiers, dotted qualified names, module
paths or whole unparsed function bodies — which is what lets the same table
normalise both the keys and the values of :func:`normalise`.
"""


def normalise_name(name: str) -> str:
    """Apply :data:`RULES` to an identifier or a dotted qualified name.

    This is how the parity guard pairs a sync function with its async twin:
    two qualified names that normalise to the same string are twins.

    Args:
        name: An identifier (``"push_async"``), a qualified name
            (``"AsyncApicSession._aiter_pages"``) or a module path
            (``"niwaki.query._async_builder"``).  Any text is accepted.

    Returns:
        The name with every rule of :data:`RULES` applied once, in order.
        A name with nothing async about it is returned unchanged.

    Example::

        >>> normalise_name("AsyncApicSession._aiter_pages")
        'ApicSession._iter_pages'
        >>> normalise_name("push_async"), normalise_name("push_sync")
        ('push', 'push')
        >>> normalise_name("AsyncNiwaki.__aenter__")
        'Niwaki.__enter__'
    """
    for pattern, replacement in RULES:
        name = pattern.sub(replacement, name)
    return name


class _Desync(ast.NodeTransformer):
    """AST transform erasing the syntactic sync/async differences, in place.

    Applied recursively (children first):

    - the docstring — a leading ``Expr`` whose value is a ``str`` constant —
      is removed from every function and class; a body left empty becomes
      ``[ast.Pass()]`` so the tree stays valid;
    - ``AsyncFunctionDef`` becomes ``FunctionDef`` (decorators, annotations
      and ``type_params`` kept);
    - ``Await(value)`` is replaced by ``value``;
    - ``AsyncWith`` becomes ``With`` and ``AsyncFor`` becomes ``For``;
    - every ``comprehension`` gets ``is_async = 0``.
    """

    @staticmethod
    def _strip_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        """Drop a leading string-constant expression; keep the body non-empty."""
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        node.body = body or [ast.Pass()]

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        self._strip_docstring(node)
        return node

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        self._strip_docstring(node)
        sync = ast.FunctionDef(
            name=node.name,
            args=node.args,
            body=node.body,
            decorator_list=node.decorator_list,
            returns=node.returns,
            type_comment=node.type_comment,
            type_params=node.type_params,
        )
        return ast.copy_location(sync, node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.generic_visit(node)
        self._strip_docstring(node)
        return node

    @override
    def visit_Await(self, node: ast.Await) -> ast.expr:
        self.generic_visit(node)
        return node.value

    @override
    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.With:
        self.generic_visit(node)
        sync = ast.With(items=node.items, body=node.body, type_comment=node.type_comment)
        return ast.copy_location(sync, node)

    @override
    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.For:
        self.generic_visit(node)
        sync = ast.For(
            target=node.target,
            iter=node.iter,
            body=node.body,
            orelse=node.orelse,
            type_comment=node.type_comment,
        )
        return ast.copy_location(sync, node)

    @override
    def visit_comprehension(self, node: ast.comprehension) -> ast.comprehension:
        self.generic_visit(node)
        node.is_async = 0
        return node


def _render(node: ast.AST) -> str:
    """Desync ``node`` (mutating it), unparse it, then apply :data:`RULES`."""
    desynced = _Desync().visit(node)
    ast.fix_missing_locations(desynced)
    return normalise_name(ast.unparse(desynced))


def normalise_code(code: str) -> str:
    """Normalise a whole module's source text.

    The source is parsed, the AST transform (async markers and docstrings
    erased) is applied to the entire tree, the tree is unparsed with
    :func:`ast.unparse`, and :data:`RULES` is applied to the resulting text.
    Comments and original formatting do not survive.

    The result is idempotent: normalising it again returns it unchanged.

    Args:
        code: Python source text of a module.

    Returns:
        The normalised source text.

    Raises:
        SyntaxError: ``code`` is not valid Python.

    Example::

        >>> normalise_code("async def f():\\n    await asyncio.sleep(1)\\n")
        'def f():\\n    time.sleep(1)'
    """
    return _render(ast.parse(code))


type _Body = list[ast.stmt]


def normalise(source: str) -> dict[str, str]:
    """Normalise every module- and class-level function of a source file.

    Every ``def``/``async def`` that is a direct statement of the module body
    or of a class body (classes nest: ``Outer.Inner.method``) yields one
    entry.  Functions nested in a function belong to their parent's body and
    are never keys; definitions under ``if``/``try`` blocks are not walked.

    Each function node is transformed (async markers and docstrings erased),
    unparsed, and passed through :data:`RULES`.  Several definitions sharing
    one qualified name — ``@overload`` stubs, a property getter and its
    setter — are joined with ``"\\n"`` in source order.

    Args:
        source: Python source text of a module.

    Returns:
        A dict, in source order, mapping each function's **original**
        (un-normalised) qualified name — ``"AsyncApicSession.login"`` — to its
        normalised text.  Keys are left as written because two classes of one
        file may normalise to the same name; pairing twins with
        :func:`normalise_name` is the caller's decision.

    Raises:
        SyntaxError: ``source`` is not valid Python.

    Example::

        >>> normalise("class AsyncS:\\n    async def get(self):\\n        await self.x()\\n")
        {'AsyncS.get': 'def get(self):\\n    self.x()'}
    """
    found: dict[str, list[str]] = {}

    def walk(body: _Body, prefix: str) -> None:
        for statement in body:
            match statement:
                case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                    found.setdefault(prefix + name, []).append(_render(statement))
                case ast.ClassDef(name=name, body=class_body):
                    walk(class_body, f"{prefix}{name}.")
                case _:
                    pass

    # The tree is freshly parsed and private to this call, so rendering each
    # function in place (the transform mutates) cannot leak anywhere.
    walk(ast.parse(source).body, "")
    return {qualname: "\n".join(bodies) for qualname, bodies in found.items()}
