"""No ``resp.json()`` result is trusted as a JSON object without a check.

``httpx.Response.json()`` returns ``Any``: whatever the body parsed to — an
object, a list, a string, ``null``.  Writing ``body: JsonObject = resp.json()``
does not check anything; it tells both type checkers to stop looking, and the
first ``body.get(...)`` on a list body then fails as a raw ``AttributeError``
far from the cause.  The transport has one function that proves the shape and
raises the typed error (``transport._errors.json_data`` → ``APIError`` on a
non-JSON or non-object body); every call site goes through it, or checks the
value itself.

The rule, enforced on the hand-written SDK (same scope as the ``Any`` guard):
a ``<expr>.json()`` call (no arguments — the httpx spelling) may only be

* assigned to a plain, unannotated name — or to a name annotated ``object`` —
* in a function that then calls ``isinstance(<that name>, ...)``.

Anything else — an annotated binding (``JsonObject``, ``dict[...]``, ...), a
``return resp.json()``, ``resp.json()`` passed straight into a call or
subscripted — is reported as ``path:line``.  The check is syntactic: it proves
an ``isinstance`` exists, not that it guards every path; the behaviour tests in
``tests/transport/test_non_object_json_bodies.py`` pin the outcome.
"""

from __future__ import annotations

import ast

import pytest

from tests.meta.test_any_budget import display_path, scoped_files


def _is_json_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "json"
        and not node.args
        and not node.keywords
    )


def _unwrap_await(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Await) else node


def _isinstance_checked(function: ast.AST, name: str) -> bool:
    """Whether *function* calls ``isinstance(name, ...)`` anywhere."""
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == name
        ):
            return True
    return False


def _bound_name(statement: ast.stmt, call: ast.Call) -> str | None:
    """The name *statement* binds *call* to, if it is an allowed binding."""
    match statement:
        case ast.Assign(targets=[ast.Name(id=name)], value=value) if _unwrap_await(value) is call:
            return name
        case ast.AnnAssign(
            target=ast.Name(id=name), annotation=ast.Name(id="object"), value=value
        ) if value is not None and _unwrap_await(value) is call:
            return name
        case _:
            return None


def _contains(container: ast.AST, target: ast.AST) -> bool:
    return any(node is target for node in ast.walk(container))


def _size(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _innermost_statement(tree: ast.AST, target: ast.AST) -> ast.stmt | None:
    """The innermost statement of *tree* that contains *target*."""
    candidates = [
        node for node in ast.walk(tree) if isinstance(node, ast.stmt) and _contains(node, target)
    ]
    return min(candidates, key=_size, default=None)


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.AST:
    """The innermost function containing *target* (the module if none)."""
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _contains(node, target)
    ]
    return min(functions, key=_size, default=tree)


def unchecked_json_calls(source: str, path: str) -> list[str]:
    """Return ``path:line`` of every ``.json()`` result used without a check.

    Args:
        source: Python source text of one module.
        path: Label used in the returned locations.

    Returns:
        Sorted ``path:line`` strings, one per offending line.

    Raises:
        SyntaxError: *source* does not parse — a file the guard cannot read
            must not pass as clean.
    """
    tree = ast.parse(source, filename=path)
    offenders: set[int] = set()
    for call in ast.walk(tree):
        if not _is_json_call(call):
            continue
        assert isinstance(call, ast.Call)
        statement = _innermost_statement(tree, call)
        name = _bound_name(statement, call) if statement is not None else None
        if name is None or not _isinstance_checked(_enclosing_function(tree, call), name):
            offenders.add(call.lineno)
    return [f"{path}:{line}" for line in sorted(offenders)]


# ── the guard on the real tree ───────────────────────────────────────────────


def test_no_unchecked_json_body_in_the_sdk() -> None:
    """Every ``.json()`` in the SDK is shape-checked before it is trusted."""
    offenders: list[str] = []
    for path in scoped_files():
        offenders.extend(unchecked_json_calls(path.read_text(encoding="utf-8"), display_path(path)))
    assert not offenders, (
        "`.json()` result trusted without a runtime check (Any laundered into a "
        "JSON type):\n  "
        + "\n  ".join(offenders)
        + "\n\nHint: use transport._errors.json_data(resp) — it raises APIError on a "
        "non-JSON or non-object body — or bind to an unannotated name and "
        "isinstance-check it."
    )


# ── the rule's own contract ──────────────────────────────────────────────────


def _scan(source: str) -> list[str]:
    return unchecked_json_calls(source, "snippet.py")


@pytest.mark.parametrize(
    "source",
    [
        "def f(resp):\n    body: JsonObject = resp.json()\n    return body\n",
        "def f(resp):\n    body: dict[str, object] = resp.json()\n"
        "    if isinstance(body, dict): ...\n",
        "def f(resp):\n    return resp.json()\n",
        "def f(resp):\n    return g(resp.json())\n",
        "def f(resp):\n    return resp.json()['imdata']\n",
        "async def f(s):\n    body: JsonObject = (await s.get()).json()\n    return body\n",
        "def f(resp):\n    body = resp.json()\n    return body.get('x')\n",
    ],
    ids=[
        "annotated-jsonobject",
        "annotated-dict-even-if-checked",
        "returned",
        "passed-to-a-call",
        "subscripted",
        "awaited-response",
        "bound-but-never-checked",
    ],
)
def test_rule_flags_unchecked_uses(source: str) -> None:
    assert _scan(source) == ["snippet.py:2"]


@pytest.mark.parametrize(
    "source",
    [
        "def f(resp):\n    data = resp.json()\n    if not isinstance(data, dict):\n"
        "        raise ValueError\n    return data\n",
        "def f(resp):\n    data: object = resp.json()\n    assert isinstance(data, dict)\n",
        "def f(resp):\n    try:\n        data = resp.json()\n    except ValueError:\n"
        "        return {}\n    if not isinstance(data, Mapping):\n        return {}\n",
        "def f(model):\n    return model.json(indent=2)\n",
        "def f(resp):\n    return json_data(resp)\n",
    ],
    ids=["checked", "object-annotated-checked", "checked-after-try", "with-arguments", "helper"],
)
def test_rule_accepts_checked_uses(source: str) -> None:
    assert _scan(source) == []


def test_rule_check_must_be_in_the_same_function() -> None:
    """An ``isinstance`` of the same name in another function proves nothing."""
    source = (
        "def f(resp):\n"
        "    data = resp.json()\n"
        "    return data\n"
        "def g(data):\n"
        "    return isinstance(data, dict)\n"
    )
    assert _scan(source) == ["snippet.py:2"]


def test_rule_refuses_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        _scan("def f(:\n")
