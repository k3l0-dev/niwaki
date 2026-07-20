"""The session-shared WebSocket behind APIC object subscription (push).

The APIC multiplexes every active subscription for a session over **one**
WebSocket (confirmed live: three concurrent subscriptions over one socket,
correctly correlated pushes) — so there must be exactly one reader loop per
session that demultiplexes incoming frames by ``subscriptionId`` into
per-subscription queues.  :class:`SubscriptionSocket` is that reader: it owns
the socket, the demux table, the refresh sweep, and reconnect-and-resubscribe.

The APIC never lets a client resume after a disconnect (no replay/"what did I
miss" primitive exists) — so reconnect always resubscribes every tracked
query from scratch under a brand-new ``subscriptionId``, and delivers a
:class:`SubscriptionGap` into every affected queue rather than silently
continuing, because a caller cannot reconcile a gap it never learns about.

``websockets`` is imported lazily, inside :meth:`SubscriptionSocket._open_socket_locked`
only — never at module import time — so ``import niwaki`` stays on its cold-start
budget until a caller actually opens a subscription.

**Resource safety net.** A forgotten ``close()`` — a crashed script, a
long-running app that never shuts its session down — must not leave an open
socket (and the server-side subscriptions the APIC holds until refresh
lapses) dangling against a production fabric. Every :class:`SubscriptionSocket`
registers a :func:`weakref.finalize` at construction that force-closes the
current socket if the object is ever garbage-collected (or the process exits)
without an explicit :meth:`~SubscriptionSocket.close`. The finalizer callback
captures only a small :class:`_SocketHandle`, never ``self`` — a callback that
held a reference to the very object it finalizes would keep it alive forever,
defeating garbage collection entirely.

**Refresh escalation is recovery, not termination.** Two consecutive missed
refreshes for one registration (RabbitMQ's own threshold for this pattern)
trigger a forced per-subscription resubscribe under a brand-new wire id — see
:meth:`SubscriptionSocket._escalate` — delivering a :class:`SubscriptionGap`
on success. ``SubscriptionLostError`` is raised for that registration only if
the resubscribe itself also fails; a struggling-but-recoverable subscription
is never killed outright, and its siblings on the same socket are never
touched. Reconnecting the shared socket itself (on an actual disconnect) uses
its own dedicated, more patient backoff policy
(``_RECONNECT_ATTEMPTS``/``_RECONNECT_WAIT_*``) rather than the session's
general-purpose per-request retry policy, since abandoning an entire
subscription is a far more consequential decision than retrying one GET.

**Bulk tools.** :meth:`SubscriptionSocket.list_subscriptions` snapshots every
tracked registration for introspection (``is_stale`` on
:class:`SubscriptionInfo` reflects ``consecutive_refresh_failures > 0``);
:meth:`SubscriptionSocket.refresh_all_subscriptions` forces an out-of-schedule
refresh sweep without ever feeding the escalation counter on failure (a
diagnostic call must not be able to kill a struggling subscription);
:meth:`SubscriptionSocket.close_all_subscriptions` stops every tracked
subscription while deliberately leaving the socket connection itself — and
its reader/refresh threads — alive and ready for a future :meth:`subscribe`,
unlike :meth:`SubscriptionSocket.close`, which tears the whole socket down.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import stamina

from niwaki import exceptions

if TYPE_CHECKING:
    from niwaki.transport.session import ApicSession

logger = logging.getLogger(__name__)

# How often the refresh-sweep thread wakes up to check which registrations
# are due — coarse on purpose, this is a background chore, not a deadline.
_SWEEP_INTERVAL_SECONDS = 1.0
# A third of the APIC's own 60 s default — the convention shared by Kafka,
# websockets, and gRPC keepalives. Chosen (rather than a half) so that two
# *consecutive* missed refreshes (see _REFRESH_FAILURE_ESCALATION_THRESHOLD)
# are detected with a full interval of margin before the APIC's own deadline
# lapses, not right at it.
_DEFAULT_REFRESH_INTERVAL_SECONDS = 20.0
# Floor for a caller-chosen refresh_timeout, so refresh-timeout=5 does not
# turn into a refresh-every-1.7s busy loop.
_MIN_REFRESH_INTERVAL_SECONDS = 5.0
# RabbitMQ's own pattern for this exact situation: one missed heartbeat is
# noise, two in a row means act. Below this, a failure is only reported as a
# SubscriptionRefreshFailed stream event; at it, _escalate() takes over.
_REFRESH_FAILURE_ESCALATION_THRESHOLD = 2
# Dedicated reconnect backoff — deliberately more patient than the session's
# general-purpose per-request RetryConfig (3 attempts / 5 s cap): abandoning
# an entire subscription is a far more consequential decision than retrying
# one GET. AWS's capped-exponential-backoff-with-jitter shape; fixed for now,
# a future parameter is the natural place to make this caller-tunable.
_RECONNECT_ATTEMPTS = 8
_RECONNECT_WAIT_INITIAL = 1.0
_RECONNECT_WAIT_MAX = 30.0
_RECONNECT_WAIT_JITTER = 1.0


def _refresh_interval(refresh_timeout: int | None) -> float:
    """The local sweep cadence for a registration's ``refresh_timeout``."""
    if refresh_timeout is None:
        return _DEFAULT_REFRESH_INTERVAL_SECONDS
    return max(_MIN_REFRESH_INTERVAL_SECONDS, refresh_timeout / 3)


# ── Raw push items — the wire shape, not yet a typed ManagedObject ────────────


@dataclass(frozen=True)
class RawSubscriptionEvent:
    """One push item straight off the wire, before any model deserialisation.

    Turning this into a typed :class:`~niwaki.models.base.ManagedObject` is the
    query layer's job (``ManagedObject.from_event``, Lot 2) — this type only
    carries what the transport layer actually knows: which subscriptions the
    event satisfies, the ACI class, and its raw string attributes.

    Attributes:
        subscription_ids: Every wire ``subscriptionId`` this single push
            satisfied — a push is an array on the wire (one fabric event can
            satisfy several active subscriptions at once) even though the
            *initial* subscribe response carries a scalar id.
        class_name: The ACI wire class name (e.g. ``"fvBD"``).
        attributes: The raw string attributes from the push payload. Confirmed
            live: a ``created`` event carries the full object; a ``modified``
            event carries only the changed properties plus ``dn``; a
            ``deleted`` event carries only ``dn`` (plus ``status``).
        status: ``"created"``, ``"modified"``, or ``"deleted"``.
    """

    subscription_ids: tuple[str, ...]
    class_name: str
    attributes: dict[str, str]
    status: str


@dataclass(frozen=True)
class SubscriptionGap:
    """A reconnect happened; events between disconnect and resubscribe may be lost.

    ACI has no replay/resume mechanism at all — Cisco's documentation and
    every piece of prior art (Cobra, acitoolkit) agree there is no "what did I
    miss since X" primitive — so a silent resubscribe would hide a real,
    uncatchable gap. This is delivered as data in the event stream instead of
    raising, because "keep going, but reconcile" is not a stream-ending
    condition; pair it with a fresh read if you need to know what changed.

    Attributes:
        disconnected_at: ``time.time()`` when the WebSocket was found dead.
        reconnected_at: ``time.time()`` when a new socket + resubscribe
            completed.
        old_subscription_id: The wire id this subscription held before the gap.
        new_subscription_id: The wire id assigned by the fresh subscribe.
    """

    disconnected_at: float
    reconnected_at: float
    old_subscription_id: str
    new_subscription_id: str


@dataclass(frozen=True)
class SubscriptionRefreshFailed:
    """A ``subscriptionRefresh`` call was rejected by the APIC.

    Informational, not fatal: Cisco does not document the client-visible
    effect of a missed refresh, and empirically a live simulator kept
    delivering events past a missed refresh window during testing. Delivered
    as data rather than raised; the client keeps refreshing on schedule
    regardless of this event, and if the subscription really is dead the next
    sign of that is a disconnect (→ :class:`SubscriptionGap`) or, once two
    consecutive refreshes fail, an automatic recovery resubscribe (also a
    :class:`SubscriptionGap`) — or, if that recovery itself fails,
    ``SubscriptionLostError``.

    Attributes:
        subscription_id: The wire id whose refresh was rejected.
        consecutive_failures: How many scheduled refreshes have failed in a
            row for this subscription, including this one. Resets to 0 on the
            next success. Reaching
            :data:`_REFRESH_FAILURE_ESCALATION_THRESHOLD` (2) triggers
            recovery instead of another one of these events.
    """

    subscription_id: str
    consecutive_failures: int = 1


@dataclass(frozen=True)
class SubscriptionInfo:
    """A point-in-time diagnostic snapshot of one tracked subscription.

    Returned by
    :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.list_subscriptions`,
    :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.refresh_all_subscriptions`,
    and :attr:`~niwaki.transport._subscription_socket.RawSubscription.info` —
    purely informational, not something to act on directly. Use
    :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.refresh_all_subscriptions`,
    :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.close_all_subscriptions`, or
    :meth:`~niwaki.transport._subscription_socket.RawSubscription.refresh_now` for that.

    Attributes:
        local_id: The stable local identity — survives a reconnect, unlike
            ``subscription_id``. What lets a caller correlate two snapshots
            taken at different times.
        subscription_id: The current wire ``subscriptionId`` — debug/
            observability only, changes across a reconnect or an escalation
            recovery.
        path: API path this subscription was opened against.
        params: Query string parameters this subscription was opened with.
        refresh_timeout: The caller-chosen refresh timeout override, if any.
        consecutive_refresh_failures: How many scheduled refreshes have
            failed in a row, right now. 0 means healthy.
        seconds_until_refresh: Time remaining until the next scheduled
            refresh sweep considers this subscription due.
    """

    local_id: int
    subscription_id: str
    path: str
    params: dict[str, str]
    refresh_timeout: int | None
    consecutive_refresh_failures: int
    seconds_until_refresh: float

    @property
    def is_stale(self) -> bool:
        """Whether this subscription has at least one recent refresh failure.

        Not based on an overdue refresh schedule — the ~1 s sweep self-heals
        that within a second, so it carries no diagnostic signal on its own.
        """
        return self.consecutive_refresh_failures > 0


RawPushItem = RawSubscriptionEvent | SubscriptionGap | SubscriptionRefreshFailed


class _Stop:
    """Sentinel: unblocks a queue consumer so a closed subscription's
    iterator ends with a plain ``StopIteration`` instead of hanging forever."""


@dataclass(frozen=True)
class _Fatal:
    """Sentinel carrying the exception to raise out of the consumer's iterator."""

    exc: Exception


_STOP = _Stop()
_QueueItem = RawPushItem | _Stop | _Fatal


@dataclass
class _Registration:
    """Everything needed to resubscribe a query from scratch after a reconnect.

    Keyed in :class:`SubscriptionSocket` by a **local** id, not the wire
    ``subscriptionId`` — the wire id changes on every reconnect, so it cannot
    be the stable identity a caller's :class:`RawSubscription` holds onto.
    """

    path: str
    params: dict[str, str]
    refresh_timeout: int | None
    wire_id: str
    queue: queue.Queue[_QueueItem] = field(default_factory=queue.Queue)
    next_refresh_at: float = 0.0
    consecutive_refresh_failures: int = 0


class RawSubscription:
    """Transport-level handle for one subscription's live event queue.

    Iterable (blocks on ``next()``/``for`` until an item arrives) and
    closeable. The query layer (Lot 2) wraps this in a typed, user-facing
    ``Subscription`` — this class only knows raw wire shapes.

    Attributes:
        initial: The synchronous snapshot from the subscribe response's own
            ``imdata`` — raw APIC envelopes, not yet parsed into events (it
            carries no ``status``, so it is not a :class:`RawSubscriptionEvent`).
    """

    def __init__(
        self,
        local_id: int,
        initial: list[dict[str, Any]],
        socket: SubscriptionSocket,
        item_queue: queue.Queue[_QueueItem],
    ) -> None:
        self.initial = initial
        self._local_id = local_id
        self._socket = socket
        self._queue = item_queue
        self._closed = False

    @property
    def subscription_id(self) -> str:
        """The current wire ``subscriptionId`` — for observability/debugging only.

        This changes across a reconnect (see :class:`SubscriptionGap`); it is
        not a stable identity to key application state on.
        """
        return self._socket._wire_id_for(self._local_id)

    @property
    def info(self) -> SubscriptionInfo:
        """Current diagnostic snapshot of this subscription.

        See :class:`SubscriptionInfo` and
        :meth:`SubscriptionSocket.list_subscriptions` for the bulk equivalent.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        info = self._socket._subscription_info_for(self._local_id)
        if info is None:
            raise exceptions.SubscriptionError(
                "cannot get info: this subscription has already been closed"
            )
        return info

    def refresh_now(self) -> SubscriptionInfo:
        """Force an immediate refresh of this subscription, outside its schedule.

        A manual refresh never feeds the automatic escalation counter on
        failure — see :meth:`SubscriptionSocket.refresh_all_subscriptions`
        for why — but a success resets it exactly like a scheduled refresh
        succeeding would.

        Returns:
            The updated :class:`SubscriptionInfo`.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        return self._socket.refresh_one_now(self._local_id)

    def __iter__(self) -> RawSubscription:
        return self

    def __next__(self) -> RawPushItem:
        """Block until the next push item, gap marker, or refresh-failure marker.

        Raises:
            StopIteration: :meth:`close` was called (from any thread).
            SubscriptionLostError: Reconnect-and-resubscribe was exhausted —
                this subscription cannot be recovered.
        """
        if self._closed:
            raise StopIteration
        item = self._queue.get()
        if isinstance(item, _Stop):
            self._closed = True
            raise StopIteration
        if isinstance(item, _Fatal):
            self._closed = True
            raise item.exc
        return item

    def close(self) -> None:
        """Stop this subscription: local bookkeeping only.

        No server-side "unsubscribe" endpoint is known to exist (undocumented,
        absent from every piece of prior art examined) — this removes the
        local queue/registration and lets a blocked ``__next__`` end cleanly.
        It does not close the session's shared WebSocket, which stays open for
        any other active subscription.
        """
        if self._closed:
            return
        self._closed = True
        self._socket.unsubscribe(self._local_id)


@dataclass
class _SocketHandle:
    """The raw resource a :func:`weakref.finalize` callback is allowed to hold.

    Deliberately independent of :class:`SubscriptionSocket` itself — the
    finalizer callback must not own a reference to the object it finalizes
    (see :func:`_finalize_socket`), so this tracks only what needs closing:
    the current live websocket connection, and whether :meth:`SubscriptionSocket.close`
    already ran (in which case there is nothing left for the finalizer to do).
    """

    ws: Any = None
    closed: bool = False


def _finalize_socket(handle: _SocketHandle) -> None:
    """The :func:`weakref.finalize` callback — force-close an abandoned socket.

    Runs when the owning :class:`SubscriptionSocket` is garbage-collected, or
    at interpreter exit if it is still alive then, **unless**
    :meth:`SubscriptionSocket.close` already ran (``handle.closed``). A no-op
    when there is nothing to close. Logs a warning either way it actually
    closes something — this path only runs when a caller did not close the
    session properly, and that is worth surfacing even though nothing here
    can raise it to the caller directly.
    """
    if handle.closed or handle.ws is None:
        return
    with contextlib.suppress(Exception):
        handle.ws.close()
    logger.warning(
        "niwaki: a subscription's WebSocket was garbage-collected without an "
        "explicit close() — closing it now. Prefer `with session:` or "
        "session.close() to shut it down deterministically, especially "
        "against a production APIC."
    )


class SubscriptionSocket:
    """One session's shared WebSocket, demultiplexed across all its subscriptions.

    Opened lazily on the first :meth:`subscribe` call. Owns exactly one
    background reader thread (``recv()`` loop → demux by wire ``subscriptionId``
    → per-subscription queue) and one refresh-sweep thread — both free
    functions holding only a :func:`weakref.ref` to this object (see
    :func:`_reader_entry`/:func:`_refresh_entry`), so neither thread keeps this
    object artificially alive; that is what lets the :func:`weakref.finalize`
    safety net (registered in :meth:`__init__`) actually collect and close an
    abandoned socket rather than only ever closing it at interpreter exit. On
    any socket error the reader thread itself reconnects (with its own
    dedicated backoff — ``_RECONNECT_ATTEMPTS``/``_RECONNECT_WAIT_*``, more
    patient than the session's general-purpose per-request retry policy) and
    resubscribes every tracked query from scratch, then delivers a
    :class:`SubscriptionGap` into each affected queue; if reconnect is
    exhausted, every tracked subscription instead receives a fatal
    ``SubscriptionLostError`` out of its iterator.

    Not part of the public API — reached through
    :meth:`~niwaki.transport.session.ApicSession.subscribe`.
    """

    def __init__(self, session: ApicSession) -> None:
        self._session = session
        self._state_lock = threading.Lock()
        self._ws: Any = None
        self._registrations: dict[int, _Registration] = {}
        self._wire_to_local: dict[str, int] = {}
        self._next_local_id = itertools.count(1)
        self._closed = False
        self._dead = False
        self._reader_thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        # Resource safety net (see the module docstring): the finalizer closes
        # an abandoned socket even if close() is never called. It receives
        # only this handle, never ``self``.
        self._handle = _SocketHandle()
        weakref.finalize(self, _finalize_socket, self._handle)

    # ── Public: subscribe / unsubscribe / close ───────────────────────────────

    def subscribe(
        self, path: str, params: dict[str, str], *, refresh_timeout: int | None = None
    ) -> RawSubscription:
        """Open the shared socket if needed, then subscribe to *path*.

        Args:
            path: API path relative to base URL, exactly as passed to a
                normal GET (e.g. ``"/api/class/fvBD.json"``).
            params: Query string parameters (filters/scoping). ``subscription``
                and ``refresh-timeout`` are added internally — do not include
                them here.
            refresh_timeout: Override the APIC's default 60 s subscription
                timeout. The subscription refreshes itself automatically on a
                schedule derived from this value regardless.

        Returns:
            A :class:`RawSubscription` — ``.initial`` for the synchronous
            snapshot, then iterate for live push items.

        Raises:
            SubscribeRejectedError: The APIC rejected the subscribe request.
        """
        self._ensure_open()
        wire_id, initial = self._do_subscribe(path, params, refresh_timeout)
        local_id = next(self._next_local_id)
        item_queue: queue.Queue[_QueueItem] = queue.Queue()
        reg = _Registration(
            path=path,
            params=dict(params),
            refresh_timeout=refresh_timeout,
            wire_id=wire_id,
            queue=item_queue,
            next_refresh_at=time.monotonic() + _refresh_interval(refresh_timeout),
        )
        with self._state_lock:
            self._registrations[local_id] = reg
            self._wire_to_local[wire_id] = local_id
        return RawSubscription(local_id, initial, self, item_queue)

    def unsubscribe(self, local_id: int) -> None:
        """Drop local bookkeeping for one subscription (see :meth:`RawSubscription.close`)."""
        with self._state_lock:
            reg = self._registrations.pop(local_id, None)
            if reg is not None:
                self._wire_to_local.pop(reg.wire_id, None)
        if reg is not None:
            reg.queue.put(_STOP)

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """Snapshot every subscription currently tracked on this socket.

        Purely informational — see :class:`SubscriptionInfo`.

        Returns:
            One :class:`SubscriptionInfo` per tracked subscription, in no
            particular order.
        """
        with self._state_lock:
            return [
                self._subscription_info(local_id, reg)
                for local_id, reg in self._registrations.items()
            ]

    def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        A diagnostic/manual tool, distinct from the automatic scheduled
        sweep: a failure here does **not** feed the escalation counter (a
        manual refresh must not be able to trigger
        :class:`~niwaki.exceptions.SubscriptionLostError` for a
        struggling-but-not-dead subscription, nor double-count against the
        scheduled sweep's own timing) — only the scheduled path escalates. A
        success still resets the counter and reschedules the next sweep tick,
        exactly as a scheduled refresh succeeding would.

        Returns:
            The post-refresh :class:`SubscriptionInfo` snapshot of every
            subscription (equivalent to calling :meth:`list_subscriptions`
            immediately after).
        """
        with self._state_lock:
            snapshot = list(self._registrations.items())
        for local_id, reg in snapshot:
            self._refresh_no_escalation(local_id, reg)
        return self.list_subscriptions()

    def refresh_one_now(self, local_id: int) -> SubscriptionInfo:
        """Force an immediate refresh of one subscription. See
        :meth:`RawSubscription.refresh_now` — this is its implementation.

        Raises:
            SubscriptionError: This subscription is no longer tracked (closed).
        """
        with self._state_lock:
            reg = self._registrations.get(local_id)
        if reg is None:
            raise exceptions.SubscriptionError(
                "cannot refresh: this subscription has already been closed"
            )
        self._refresh_no_escalation(local_id, reg)
        info = self._subscription_info_for(local_id)
        if info is None:
            raise exceptions.SubscriptionError(
                "cannot refresh: this subscription was closed during the refresh"
            )
        return info

    def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the socket itself stays open.

        Distinct from :meth:`close`: every blocked :class:`RawSubscription`
        iterator wakes with a plain ``StopIteration``, exactly as ``close()``
        does, but the WebSocket connection, the reader thread, and the
        refresh-sweep thread are left running untouched — a later
        :meth:`subscribe` reuses the same still-open socket immediately, with
        no reconnect needed. Idempotent: calling this again with nothing left
        tracked is a no-op. Safe with respect to the resource safety net (see
        the module docstring) — the socket handle is untouched, so an
        abandoned session still gets it closed eventually.
        """
        with self._state_lock:
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
        for reg in regs:
            reg.queue.put(_STOP)

    def close(self) -> None:
        """Tear down the socket and every tracked subscription.

        Called by :meth:`~niwaki.transport.session.ApicSession.close`. Every
        blocked :class:`RawSubscription` iterator wakes with a plain
        ``StopIteration`` rather than hanging.
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._handle.closed = True
            ws = self._ws
            self._ws = None
            self._handle.ws = None
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
        for reg in regs:
            reg.queue.put(_STOP)
        if ws is not None:
            with contextlib.suppress(Exception):  # best-effort — we are tearing down anyway
                ws.close()

    def _wire_id_for(self, local_id: int) -> str:
        with self._state_lock:
            reg = self._registrations.get(local_id)
            return reg.wire_id if reg is not None else ""

    @staticmethod
    def _subscription_info(local_id: int, reg: _Registration) -> SubscriptionInfo:
        """Build a :class:`SubscriptionInfo` snapshot. Caller must hold ``_state_lock``."""
        return SubscriptionInfo(
            local_id=local_id,
            subscription_id=reg.wire_id,
            path=reg.path,
            params=dict(reg.params),
            refresh_timeout=reg.refresh_timeout,
            consecutive_refresh_failures=reg.consecutive_refresh_failures,
            seconds_until_refresh=max(0.0, reg.next_refresh_at - time.monotonic()),
        )

    def _subscription_info_for(self, local_id: int) -> SubscriptionInfo | None:
        with self._state_lock:
            reg = self._registrations.get(local_id)
            return self._subscription_info(local_id, reg) if reg is not None else None

    # ── Internal: the REST half of subscribe/refresh ──────────────────────────

    def _do_subscribe(
        self, path: str, params: dict[str, str], refresh_timeout: int | None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Issue the ``subscription=yes`` GET and return ``(wire_id, initial_imdata)``."""
        full_params: dict[str, str] = {**params, "subscription": "yes"}
        if refresh_timeout is not None:
            full_params["refresh-timeout"] = str(refresh_timeout)
        try:
            resp = self._session._request_checked(path, full_params)
        except exceptions.APIError as exc:
            raise exceptions.SubscribeRejectedError(exc.status_code, exc.apic_message) from exc
        body: dict[str, Any] = resp.json()
        wire_id = body.get("subscriptionId")
        if not wire_id:
            raise exceptions.SubscribeRejectedError(
                resp.status_code, "APIC response carried no subscriptionId"
            )
        return str(wire_id), list(body.get("imdata", []))

    # ── Internal: socket lifecycle ─────────────────────────────────────────────

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._ws is not None:
                return
            self._open_socket_locked()
            self._dead = False
            # The thread targets hold only a *weak* reference (see
            # ``_reader_entry``/``_refresh_entry``) — a bound method here would
            # keep ``self`` permanently alive for as long as either thread
            # runs, defeating the weakref.finalize safety net entirely (the
            # object could never become unreachable while its own reader
            # thread is blocked in recv(), which is most of the time).
            ref = weakref.ref(self)
            self._reader_thread = threading.Thread(
                target=_reader_entry, args=(ref,), name="niwaki-subscription-reader", daemon=True
            )
            self._reader_thread.start()
            self._refresh_thread = threading.Thread(
                target=_refresh_entry, args=(ref,), name="niwaki-subscription-refresh", daemon=True
            )
            self._refresh_thread.start()

    def _open_socket_locked(self) -> None:
        """Open the WebSocket. Caller must hold ``_state_lock``."""
        self._session._ensure_token()  # fresh token before it goes in the URL
        token = self._session._token_state.token  # type: ignore[union-attr]
        parsed = urlsplit(self._session._host)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{scheme}://{parsed.netloc}/socket{token}"
        import websockets.sync.client as ws_client  # lazy: protects the cold-start budget

        self._ws = ws_client.connect(url, ssl=self._session._ws_ssl_context)
        # Keep the safety-net handle pointing at the *current* socket across
        # every (re)connect — it is what the finalizer callback can safely see.
        self._handle.ws = self._ws

    # ── Internal: dispatch (called from the free-function reader loop) ───────

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            data: dict[str, Any] = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("niwaki: malformed subscription push frame, ignored")
            return
        wire_ids_raw = data.get("subscriptionId") or []
        wire_ids = [wire_ids_raw] if isinstance(wire_ids_raw, str) else list(wire_ids_raw)
        imdata = data.get("imdata") or []

        with self._state_lock:
            target_queues = [
                self._registrations[local_id].queue
                for wire_id in wire_ids
                if (local_id := self._wire_to_local.get(wire_id)) is not None
                and local_id in self._registrations
            ]
        if not target_queues:
            return

        for item in imdata:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            ((class_name, body),) = item.items()
            attrs: dict[str, str] = (
                (body or {}).get("attributes", {}) if isinstance(body, dict) else {}
            )
            event = RawSubscriptionEvent(
                subscription_ids=tuple(wire_ids),
                class_name=class_name,
                attributes=dict(attrs),
                status=attrs.get("status", "modified"),
            )
            for q in target_queues:
                q.put(event)

    # ── Internal: refresh sweep (entry point is the free-function loop) ──────

    def _do_refresh(self, reg: _Registration) -> bool:
        """Issue the ``subscriptionRefresh`` GET. No bookkeeping — callers own that."""
        try:
            self._session._request_checked("/api/subscriptionRefresh.json", {"id": reg.wire_id})
        except exceptions.APIError:
            logger.warning(
                "niwaki: subscription refresh rejected (path=%s, id=%s)", reg.path, reg.wire_id
            )
            return False
        return True

    def _refresh_no_escalation(self, local_id: int, reg: _Registration) -> bool:
        """A manual refresh (bulk or single) — never escalates, only the
        scheduled sweep (:meth:`_refresh_one`) does. A success still resets
        the counter and reschedules; a failure is left untouched entirely,
        so it neither feeds escalation nor disturbs the scheduled sweep's own
        timing for this registration.
        """
        ok = self._do_refresh(reg)
        with self._state_lock:
            if ok and local_id in self._registrations:
                reg.consecutive_refresh_failures = 0
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
        return ok

    def _refresh_one(self, local_id: int, reg: _Registration) -> None:
        """The scheduled refresh path — the only one that can escalate.

        On success, resets the failure counter. On failure, increments it and
        either emits a :class:`SubscriptionRefreshFailed` event (below the
        escalation threshold) or hands off to :meth:`_escalate` (at it).
        """
        ok = self._do_refresh(reg)
        still_tracked = False
        failures = 0
        with self._state_lock:
            if local_id in self._registrations:
                still_tracked = True
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
                reg.consecutive_refresh_failures = 0 if ok else reg.consecutive_refresh_failures + 1
                failures = reg.consecutive_refresh_failures
        if not still_tracked or ok:
            return
        if failures >= _REFRESH_FAILURE_ESCALATION_THRESHOLD:
            self._escalate(local_id, reg)
        else:
            reg.queue.put(
                SubscriptionRefreshFailed(
                    subscription_id=reg.wire_id, consecutive_failures=failures
                )
            )

    def _escalate(self, local_id: int, reg: _Registration) -> None:
        """Force a per-subscription resubscribe after consecutive missed refreshes.

        Recovery, not termination: called only once
        ``consecutive_refresh_failures`` reaches
        ``_REFRESH_FAILURE_ESCALATION_THRESHOLD``. A fresh subscribe under a
        new wire id is attempted first; ``SubscriptionLostError`` is raised
        only if that attempt *also* fails — mirroring how a full socket
        reconnect already treats each registration individually in
        :meth:`_reconnect_and_resubscribe_all`. The socket connection itself
        is never touched by this path.
        """
        escalated_at = time.time()
        old_wire = reg.wire_id
        try:
            new_wire, _initial = self._do_subscribe(reg.path, reg.params, reg.refresh_timeout)
        except exceptions.SubscriptionError as exc:
            with self._state_lock:
                if local_id not in self._registrations or reg.wire_id != old_wire:
                    # Already resubscribed by a concurrent full reconnect, or
                    # removed by unsubscribe()/close_all_subscriptions() — the
                    # registration's fate is someone else's to decide now.
                    return
                self._wire_to_local.pop(old_wire, None)
                self._registrations.pop(local_id, None)
            reg.queue.put(
                _Fatal(
                    exceptions.SubscriptionLostError(
                        f"subscription refresh failed {_REFRESH_FAILURE_ESCALATION_THRESHOLD} "
                        f"times consecutively and the recovery resubscribe also failed: {exc}",
                        reason=exceptions.SubscriptionLostReason.REFRESH_ESCALATION,
                    )
                )
            )
            return
        with self._state_lock:
            if local_id not in self._registrations:
                # unsubscribe()/close_all_subscriptions() won the race.
                return
            if reg.wire_id != old_wire:
                # The reader's own full reconnect already resubscribed this
                # registration under a fresher id — do not clobber it.
                return
            self._wire_to_local.pop(old_wire, None)
            self._wire_to_local[new_wire] = local_id
            reg.wire_id = new_wire
            reg.consecutive_refresh_failures = 0
            reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
        reg.queue.put(
            SubscriptionGap(
                disconnected_at=escalated_at,
                reconnected_at=time.time(),
                old_subscription_id=old_wire,
                new_subscription_id=new_wire,
            )
        )

    # ── Internal: reconnect ─────────────────────────────────────────────────────

    def _reconnect_and_resubscribe_all(self) -> bool:
        """Reconnect the socket and resubscribe every tracked query from scratch.

        Returns:
            ``True`` if the socket itself came back (individual registrations
            may still have failed to resubscribe and been dropped with their
            own fatal marker). ``False`` if the socket could not be reconnected
            at all — every tracked registration has already been failed with
            ``SubscriptionLostError`` and the reader loop must stop.
        """
        import websockets.exceptions as ws_exceptions

        logger.warning("niwaki: subscription websocket disconnected — reconnecting")
        disconnected_at = time.time()
        try:
            for attempt in stamina.retry_context(
                on=(ws_exceptions.WebSocketException, OSError),
                attempts=_RECONNECT_ATTEMPTS,
                wait_initial=_RECONNECT_WAIT_INITIAL,
                wait_max=_RECONNECT_WAIT_MAX,
                wait_jitter=_RECONNECT_WAIT_JITTER,
            ):
                with attempt, self._state_lock:
                    # close() may have run while we were retrying — stop,
                    # rather than reopen a socket nobody will ever close.
                    if self._closed:
                        return False
                    self._open_socket_locked()
        except (ws_exceptions.WebSocketException, OSError) as exc:
            with self._state_lock:
                if self._closed:
                    return False
            self._fail_all(
                exceptions.SubscriptionLostError(
                    f"subscription websocket lost, reconnect exhausted: {exc}",
                    reason=exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED,
                )
            )
            return False

        with self._state_lock:
            if self._closed:
                # close() raced with a reconnect that just succeeded — the
                # fresh socket is not needed; close it and stop, exactly as
                # close() itself would have, had it won the race outright.
                orphaned_ws = self._ws
                self._ws = None
                with contextlib.suppress(Exception):
                    orphaned_ws.close()
                return False
            snapshot = dict(self._registrations)
        reconnected_at = time.time()
        for local_id, reg in snapshot.items():
            old_wire = reg.wire_id
            try:
                new_wire, _initial = self._do_subscribe(reg.path, reg.params, reg.refresh_timeout)
            except exceptions.SubscriptionError as exc:
                with self._state_lock:
                    self._wire_to_local.pop(old_wire, None)
                    self._registrations.pop(local_id, None)
                reg.queue.put(
                    _Fatal(
                        exceptions.SubscriptionLostError(
                            f"could not resubscribe after reconnect: {exc}",
                            reason=exceptions.SubscriptionLostReason.RESUBSCRIBE_FAILED,
                        )
                    )
                )
                continue
            with self._state_lock:
                self._wire_to_local.pop(old_wire, None)
                self._wire_to_local[new_wire] = local_id
                reg.wire_id = new_wire
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
            reg.queue.put(
                SubscriptionGap(
                    disconnected_at=disconnected_at,
                    reconnected_at=reconnected_at,
                    old_subscription_id=old_wire,
                    new_subscription_id=new_wire,
                )
            )
        return True

    def _fail_all(self, exc: exceptions.SubscriptionLostError) -> None:
        with self._state_lock:
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
            self._ws = None
            # Not handle.closed = True: a fresh subscribe() later must still
            # be able to reopen (see _ensure_open), and the finalizer should
            # cover that new socket too if it is ever abandoned in turn.
            self._handle.ws = None
            self._dead = True
        for reg in regs:
            reg.queue.put(_Fatal(exc))


# ── Thread entry points (free functions — weak reference only) ───────────────
#
# These hold a weakref.ref, not `self`, and resolve it fresh on each loop
# iteration, never across a blocking call (recv()/sleep()). A bound method
# (self._read_loop) would instead keep the SubscriptionSocket permanently
# reachable for as long as the thread runs — almost always, since the reader
# is blocked in recv() most of the time — which would make it uncollectable
# and defeat the weakref.finalize safety net (see the module docstring)
# entirely: an abandoned session's socket would then only ever get closed at
# interpreter exit (the finalizer's atexit fallback), never while the process
# that abandoned it keeps running.


def _reader_entry(ref: weakref.ReferenceType[SubscriptionSocket]) -> None:
    """Reader-thread entry point. See the module note above on why this is a
    free function taking a weak reference, not a bound method."""
    import websockets.exceptions as ws_exceptions

    while True:
        socket = ref()
        if socket is None:
            return
        with socket._state_lock:
            if socket._closed:
                return
            ws = socket._ws
        del socket  # never hold a strong reference across the blocking recv()
        try:
            raw = ws.recv()
        except (ws_exceptions.WebSocketException, OSError):
            socket = ref()
            if socket is None:
                return
            with socket._state_lock:
                if socket._closed:
                    return
            if not socket._reconnect_and_resubscribe_all():
                return
            del socket
            continue
        socket = ref()
        if socket is None:
            return
        socket._dispatch(raw)
        del socket


def _refresh_entry(ref: weakref.ReferenceType[SubscriptionSocket]) -> None:
    """Refresh-sweep thread entry point. See the module note above on why
    this is a free function taking a weak reference, not a bound method."""
    while True:
        time.sleep(_SWEEP_INTERVAL_SECONDS)
        socket = ref()
        if socket is None:
            return
        with socket._state_lock:
            if socket._closed or socket._dead:
                return
            now = time.monotonic()
            due = [
                (local_id, reg)
                for local_id, reg in socket._registrations.items()
                if reg.next_refresh_at <= now
            ]
        for local_id, reg in due:
            socket._refresh_one(local_id, reg)
        del socket
