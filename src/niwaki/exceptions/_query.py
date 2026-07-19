"""Query execution errors.

Raised by the single-object executor :meth:`~niwaki.query.Query.one`, which
demands exactly one match.  Both are direct children of
:class:`~niwaki.exceptions.NiwakiError` — the precedent is Django's
``DoesNotExist`` / ``MultipleObjectsReturned`` — so a caller can target the
outcome precisely rather than reaching for the HTTP-flavoured
:class:`~niwaki.exceptions.NotFoundError` (which carries a status code and a
different meaning).
"""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class NoResultError(NiwakiError):
    """A query that required exactly one object matched none.

    Raised by :meth:`~niwaki.query.Query.one` /
    :meth:`~niwaki.query.AsyncQuery.one` when the result set is empty.  Use
    :meth:`~niwaki.query.Query.first` when *no match* is an acceptable outcome.
    """


class MultipleResultsError(NiwakiError):
    """A query that required exactly one object matched more than one.

    Raised by :meth:`~niwaki.query.Query.one` /
    :meth:`~niwaki.query.AsyncQuery.one` when the result set holds two or more
    objects.  Narrow the query, or use :meth:`~niwaki.query.Query.first` /
    :meth:`~niwaki.query.Query.fetch` when several matches are expected.
    """
