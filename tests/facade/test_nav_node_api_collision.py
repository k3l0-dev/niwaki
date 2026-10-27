"""Navigation under ``callhomeQueryGroup`` — the edge the node API used to shadow.

Until 2.0.1 the curated maker name of ``callhomeQuery`` under
``callhomeQueryGroup`` was ``query``.  The navigation surface speaks the curated
vocabulary, so ``CHILD_MAP["callhomeQueryGroup"]["query"]`` was emitted — but
``NiwakiNode.query`` is a real method, attribute lookup never reaches
``__getattr__`` for it, and the edge was unreachable at runtime (only ``.mo()``
got there).

The chosen name is ``callhome_query``:

- the catalogue label of ``callhomeQuery`` is the bare ``"Query"`` — a word the
  node API owns, so the label alone cannot be the name;
- the parent's own curated name is ``callhome_query_group``, and the curated
  siblings of the other call-home groups follow the same ``callhome_<label>``
  shape (``callhome_destination``, ``callhome_protocol_profile``):
  ``callhome_query`` is the child of ``callhome_query_group`` exactly as an
  operator reads it in the GUI ("Callhome Query Group" → "Query");
- ``smart_query`` was rejected: ``smart_`` is the curated marker of the
  *Smart* Call Home family (``callhomeSmartGroup`` →
  ``smart_callhome_destination``), which this class does not belong to.

The rename lives in the curated vocabulary (``vocabulary.yaml`` makers), so the
design DSL and the navigation keep speaking one name.  No ``NAV_DEPRECATED``
alias is emitted for ``query``: the old name never resolved, and an alias would
be shadowed by the very same method.

All tests run without a session: navigation computes DNs locally.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from niwaki import AsyncNiwaki, Niwaki
from niwaki.design import fabric
from niwaki.design._cursor import _tables
from niwaki.domain._child_map import CHILD_MAP, NAV_DEPRECATED
from niwaki.facade import AsyncNiwakiNode, NiwakiNode
from niwaki.models._generated.callhome.callhomeQuery import callhomeQuery
from niwaki.models._generated.callhome.callhomeQueryGroup import callhomeQueryGroup
from niwaki.query._async_builder import AsyncQuery
from niwaki.query._builder import Query

NEW_NAME = "callhome_query"
GROUP_DN = "uni/fabric/chquerygroup-g"
QUERY_DN = f"{GROUP_DN}/chquery-q"


def _sync_group() -> NiwakiNode[callhomeQueryGroup]:
    return NiwakiNode(Niwaki(), GROUP_DN, callhomeQueryGroup)


def _async_group() -> AsyncNiwakiNode[callhomeQueryGroup]:
    return AsyncNiwakiNode(AsyncNiwaki(), GROUP_DN, callhomeQueryGroup)


class TestGeneratedTables:
    def test_new_name_is_the_edge(self) -> None:
        assert CHILD_MAP["callhomeQueryGroup"][NEW_NAME] == "callhomeQuery"

    def test_old_name_is_gone_from_the_row(self) -> None:
        assert "query" not in CHILD_MAP["callhomeQueryGroup"]

    def test_callhome_query_has_exactly_one_edge_under_the_group(self) -> None:
        row = CHILD_MAP["callhomeQueryGroup"]
        assert [n for n, c in row.items() if c == "callhomeQuery"] == [NEW_NAME]

    def test_no_deprecated_alias_for_the_old_name(self) -> None:
        """The old name never worked; an alias would be shadowed all the same."""
        assert "query" not in NAV_DEPRECATED.get("callhomeQueryGroup", {})

    def test_rename_is_curated_in_the_vocabulary(self) -> None:
        makers = _tables().makers["callhomeQueryGroup"]
        assert makers.get(NEW_NAME) == "callhomeQuery"
        assert "query" not in makers


class TestSyncNavigation:
    def test_new_name_resolves_to_a_callhome_query_node(self) -> None:
        node = _sync_group().callhome_query("q")
        assert isinstance(node, NiwakiNode)
        assert node.cls is callhomeQuery
        assert node.dn == QUERY_DN

    def test_new_name_matches_mo(self) -> None:
        group = _sync_group()
        assert group.callhome_query("q").dn == group.mo(callhomeQuery, name="q").dn

    def test_reached_from_the_client_root(self) -> None:
        node = Niwaki().fabric().callhome_query_group("g").callhome_query("q")
        assert node.cls is callhomeQuery
        assert node.dn == QUERY_DN

    def test_new_name_without_a_name_lists_the_class(self) -> None:
        aci = Niwaki()
        aci._session = MagicMock()  # pyright: ignore[reportPrivateUsage]
        listing = NiwakiNode(aci, GROUP_DN, callhomeQueryGroup).callhome_query()
        assert isinstance(listing, Query)
        _path, params = listing.build()
        assert params["target-subtree-class"] == "callhomeQuery"

    def test_query_is_still_the_query_method(self) -> None:
        group = _sync_group()
        assert group.query.__func__ is NiwakiNode.query

    def test_query_still_builds_a_scoped_query(self) -> None:
        aci = Niwaki()
        aci._session = MagicMock()  # pyright: ignore[reportPrivateUsage]
        query = NiwakiNode(aci, GROUP_DN, callhomeQueryGroup).query("faultInst")
        assert isinstance(query, Query)
        path, params = query.build()
        assert GROUP_DN in path
        assert params["target-subtree-class"] == "faultInst"


class TestAsyncNavigation:
    def test_new_name_resolves_to_a_callhome_query_node(self) -> None:
        node = _async_group().callhome_query("q")
        assert isinstance(node, AsyncNiwakiNode)
        assert node.cls is callhomeQuery
        assert node.dn == QUERY_DN

    def test_reached_from_the_client_root(self) -> None:
        node = AsyncNiwaki().fabric().callhome_query_group("g").callhome_query("q")
        assert node.cls is callhomeQuery
        assert node.dn == QUERY_DN

    def test_query_is_still_the_query_method(self) -> None:
        group = _async_group()
        assert group.query.__func__ is AsyncNiwakiNode.query

    def test_query_still_builds_a_scoped_query(self) -> None:
        aci = AsyncNiwaki()
        aci._session = MagicMock()  # pyright: ignore[reportPrivateUsage]
        query = AsyncNiwakiNode(aci, GROUP_DN, callhomeQueryGroup).query("faultInst")
        assert isinstance(query, AsyncQuery)


class TestDesignSpeaksTheSameName:
    """The curated rename reaches the design DSL: one vocabulary, both sides."""

    def test_maker_declares_a_callhome_query(self) -> None:
        cursor = fabric().callhome_query_group("g").callhome_query("q")
        assert cursor.dn == QUERY_DN
