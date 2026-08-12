"""The async mirror of :mod:`niwaki.transport._subscription_socket`.

Same protocol, same guarantees (one shared WebSocket per session, no
replay/resume ever, reconnect delivers a ``SubscriptionGap`` rather than
silently continuing) — read that module's docstring first. This module
exists because the concurrency primitives differ throughout:
``asyncio.Lock``/``asyncio.Queue``/``asyncio.Task`` instead of
``threading.Lock``/``queue.Queue``/``threading.Thread``,
``websockets.asyncio.client`` instead of ``websockets.sync.client``. The wire
item types (:class:`~niwaki.transport._subscription_socket.RawSubscriptionEvent`,
``SubscriptionGap``, ``SubscriptionRefreshFailed``) have no sync/async
coupling at all and are reused directly from the sync module rather than
duplicated.

**Resource safety net — a genuinely different mechanism, not a decoration.**
The sync socket's finalizer calls the synchronous ``ws.close()``. That does
not exist here: :meth:`~websockets.asyncio.client.ClientConnection.close` is
a coroutine, and a :func:`weakref.finalize` callback runs synchronously,
wherever/whenever it fires (a GC pass or interpreter shutdown) — **not**
guaranteed to run on the event loop's thread, or even inside a running loop
iteration. Verified empirically against this project's pinned
``websockets`` (16.1.1): ``transport.abort()`` is unsafe here (it schedules
work via ``call_soon``/``_remove_reader``, neither thread-safe off-loop);
closing the raw socket fd directly does not wake a suspended ``recv()`` on
every platform (confirmed silent on kqueue/macOS). The one primitive that is
genuinely loop-independent, safe from any thread or context, releases the
APIC-side resource, *and* wakes the reader task's ``recv()`` is a plain
socket syscall: ``TransportSocket.shutdown(socket.SHUT_RDWR)`` on the socket
``get_extra_info("socket")`` returns. The safety-net handle therefore stores
a raw socket, not a connection object.

The same free-function-holding-only-a-weakref pattern the sync module uses
for its reader/refresh threads applies identically here for
``asyncio.Task``: a coroutine created from a bound method
(``asyncio.create_task(self._reader_loop())``) keeps ``self`` reachable in
the suspended frame for as long as the task lives — mostly always, since the
reader spends nearly all its time parked at ``await ws.recv()`` — which would
defeat the finalizer exactly as the sync bound-method bug did. Verified
empirically: dropping the only reference and calling ``gc.collect()``
collects the socket (and fires the finalizer) while the reader task is still
alive, provided the task holds only a :func:`weakref.ref`, resolved fresh
each loop iteration and never held across an ``await``.

Refresh escalation, the dedicated reconnect backoff, and the bulk
introspection/refresh/stop tools all mirror the sync module exactly — see
its docstring — with the escalation counter's mutation and the reconnect
retry both awaited rather than blocking.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
import weakref
from dataclasses import dataclass, field
from socket import SHUT_RDWR
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import stamina

from niwaki import exceptions
from niwaki.transport._subscription_socket import (
    _DEFAULT_MAX_PENDING,
    _RECONNECT_ATTEMPTS,
    _RECONNECT_WAIT_INITIAL,
    _RECONNECT_WAIT_JITTER,
    _RECONNECT_WAIT_MAX,
    _REFRESH_FAILURE_ESCALATION_THRESHOLD,
    _STOP,
    _SWEEP_INTERVAL_SECONDS,
    RawPushItem,
    RawSubscriptionEvent,
    SubscriptionGap,
    SubscriptionInfo,
    SubscriptionOverflow,
    SubscriptionRefreshFailed,
    _Fatal,
    _QueueItem,
    _refresh_interval,
    _Stop,
    _validate_refresh_timeout,
)

if TYPE_CHECKING:
    from niwaki.transport.session_async import AsyncApicSession

logger = logging.getLogger(__name__)


@dataclass
class _AsyncRegistration:
    """Async counterpart of :class:`~niwaki.transport._subscription_socket._Registration`
    — identical fields, an ``asyncio.Queue`` instead of a ``queue.Queue``."""

    path: str
    params: dict[str, str]
    refresh_timeout: int | None
    wire_id: str
    queue: asyncio.Queue[_QueueItem] = field(default_factory=asyncio.Queue)
    next_refresh_at: float = 0.0
    consecutive_refresh_failures: int = 0
    # Backpressure — same policy as the sync registration: the queue object
    # stays unbounded, the bound applies to data events only, single-writer
    # bookkeeping (the reader task).
    max_pending: int = _DEFAULT_MAX_PENDING
    dropped_total: int = 0
    overflow_open: bool = False


class AsyncRawSubscription:
    """Async counterpart of :class:`~niwaki.transport._subscription_socket.RawSubscription`.

    Attributes:
        initial: The subscribe response's own synchronous snapshot — see the
            sync class for the full explanation.
    """

    def __init__(
        self,
        local_id: int,
        initial: list[dict[str, Any]],
        socket: AsyncSubscriptionSocket,
        item_queue: asyncio.Queue[_QueueItem],
    ) -> None:
        self.initial = initial
        self._local_id = local_id
        self._socket = socket
        self._queue = item_queue
        self._closed = False
        # Vanished-consumer safety net — the async unsubscribe is a
        # coroutine and the GC can fire this on any thread, so the callback
        # hops onto the socket's loop with call_soon_threadsafe.
        self._finalizer = weakref.finalize(
            self,
            _finalize_async_raw_subscription,
            weakref.ref(socket),
            asyncio.get_running_loop(),
            local_id,
        )

    @property
    def subscription_id(self) -> str:
        """The current wire ``subscriptionId`` — for observability/debugging only.

        See :attr:`~niwaki.transport._subscription_socket.RawSubscription.subscription_id`.
        """
        return self._socket._wire_id_for(self._local_id)

    @property
    def info(self) -> SubscriptionInfo:
        """Current diagnostic snapshot of this subscription.

        See :attr:`~niwaki.transport._subscription_socket.RawSubscription.info`.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        info = self._socket._subscription_info_for(self._local_id)
        if info is None:
            raise exceptions.SubscriptionError(
                "cannot get info: this subscription has already been closed"
            )
        return info

    async def refresh_now(self) -> SubscriptionInfo:
        """Force an immediate refresh of this subscription, outside its schedule.

        See :meth:`~niwaki.transport._subscription_socket.RawSubscription.refresh_now`.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        return await self._socket.refresh_one_now(self._local_id)

    def __aiter__(self) -> AsyncRawSubscription:
        return self

    async def __anext__(self) -> RawPushItem:
        """Await the next push item, gap marker, or refresh-failure marker.

        Raises:
            StopAsyncIteration: :meth:`close` was called (from any task).
            SubscriptionLostError: Reconnect-and-resubscribe was exhausted —
                this subscription cannot be recovered.
        """
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if isinstance(item, _Stop):
            self._closed = True
            raise StopAsyncIteration
        if isinstance(item, _Fatal):
            self._closed = True
            raise item.exc
        return item

    async def close(self) -> None:
        """Stop this subscription: local bookkeeping only.

        See :meth:`~niwaki.transport._subscription_socket.RawSubscription.close`
        — the same "no server-side unsubscribe endpoint" caveat applies.
        """
        if self._closed:
            return
        self._closed = True
        self._finalizer.detach()
        await self._socket.unsubscribe(self._local_id)


@dataclass
class _AsyncSocketHandle:
    """The raw resource a :func:`weakref.finalize` callback is allowed to hold.

    Stores a raw ``socket.socket`` (not the async connection object) — see
    the module docstring for why ``shutdown()`` on the raw socket is the only
    primitive that is safe to call from a finalizer callback's arbitrary
    context. Deliberately independent of :class:`AsyncSubscriptionSocket`
    itself, for the same reason as the sync ``_SocketHandle``.
    """

    sock: Any = None
    closed: bool = False


def _finalize_async_raw_subscription(
    socket_ref: weakref.ref[AsyncSubscriptionSocket],
    loop: asyncio.AbstractEventLoop,
    local_id: int,
) -> None:
    """Safety-net callback: a consumer vanished without ``close()``.

    Mirror of the sync :func:`~niwaki.transport._subscription_socket._finalize_raw_subscription`
    — but the GC may fire it on any thread, so the registration drop hops
    onto the socket's loop; if the loop is already closed there is nothing
    left to leak (the socket finalizer covers the raw connection)."""
    socket = socket_ref()
    if socket is None:
        return
    # GC context: no locks here — not even logging's. The warning is
    # emitted from the loop callback instead.
    with contextlib.suppress(RuntimeError):  # loop closed — nothing to drop anymore
        loop.call_soon_threadsafe(socket._drop_registration_nowait, local_id, True)


def _finalize_async_socket(handle: _AsyncSocketHandle) -> None:
    """The :func:`weakref.finalize` callback — force-close an abandoned socket.

    See :func:`~niwaki.transport._subscription_socket._finalize_socket` for
    the sync twin; this uses ``shutdown(SHUT_RDWR)`` on the raw socket
    instead of an async ``close()``, since this callback cannot safely await
    anything or assume it runs on the event loop's thread.
    """
    if handle.closed or handle.sock is None:
        return
    with contextlib.suppress(Exception):
        handle.sock.shutdown(SHUT_RDWR)
    logger.warning(
        "niwaki: a subscription's WebSocket was garbage-collected without an "
        "explicit close() — closing it now. Prefer `async with session:` or "
        "await session.close() to shut it down deterministically, especially "
        "against a production APIC."
    )


class AsyncSubscriptionSocket:
    """Async counterpart of :class:`~niwaki.transport._subscription_socket.SubscriptionSocket`.

    Owns one background reader task and one refresh-sweep task — both free
    coroutine functions holding only a :func:`weakref.ref` (see
    :func:`_reader_entry`/:func:`_refresh_entry`), for the same reason the
    sync socket's threads do: so the object stays collectable, and the
    :func:`weakref.finalize` safety net registered in :meth:`__init__`
    actually fires while abandoned rather than only at interpreter exit.

    Not part of the public API — reached through
    :meth:`~niwaki.transport.session_async.AsyncApicSession.subscribe`.
    """

    def __init__(self, session: AsyncApicSession) -> None:
        self._session = session
        self._state_lock = asyncio.Lock()
        self._ws: Any = None
        self._registrations: dict[int, _AsyncRegistration] = {}
        self._wire_to_local: dict[str, int] = {}
        self._next_local_id = itertools.count(1)
        self._closed = False
        self._dead = False
        # Monotonic generation of the reader/refresh task pair — same
        # mechanism as the sync socket: bumped by _ensure_open() when a fresh
        # pair starts and by _fail_all() when the current pair must die, so a
        # task from a superseded generation exits on its next wakeup instead
        # of running alongside the fresh pair (double-counting failures).
        self._generation = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._handle = _AsyncSocketHandle()
        weakref.finalize(self, _finalize_async_socket, self._handle)

    # ── Public: subscribe / unsubscribe / close ───────────────────────────────

    async def subscribe(
        self,
        path: str,
        params: dict[str, str],
        *,
        refresh_timeout: int | None = None,
        max_pending: int = _DEFAULT_MAX_PENDING,
    ) -> AsyncRawSubscription:
        """Open the shared socket if needed, then subscribe to *path*.

        See :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.subscribe`.

        Raises:
            ValueError: *refresh_timeout* is below the floor the refresh
                machinery can mathematically honour (see
                :func:`~niwaki.transport._subscription_socket._validate_refresh_timeout`).
            SubscriptionError: :meth:`aclose` already ran on this socket —
                including an ``aclose()`` that raced this very call.
            SubscribeRejectedError: The APIC rejected the subscribe request.
        """
        _validate_refresh_timeout(refresh_timeout)
        await self._ensure_open()
        wire_id, initial = await self._do_subscribe(path, params, refresh_timeout)
        local_id = next(self._next_local_id)
        item_queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        reg = _AsyncRegistration(
            path=path,
            params=dict(params),
            refresh_timeout=refresh_timeout,
            wire_id=wire_id,
            queue=item_queue,
            next_refresh_at=time.monotonic() + _refresh_interval(refresh_timeout),
            max_pending=max_pending,
        )
        async with self._state_lock:
            if self._closed:
                # aclose() ran between our REST subscribe and this insertion.
                # Registering now would create a queue no _STOP ever reaches —
                # a consumer blocked forever. The server-side subscription
                # lapses on its own at refresh-timeout (there is no
                # unsubscribe endpoint to call).
                raise exceptions.SubscriptionError(
                    "cannot subscribe: the subscription socket has been closed"
                )
            self._registrations[local_id] = reg
            self._wire_to_local[wire_id] = local_id
        return AsyncRawSubscription(local_id, initial, self, item_queue)

    def _drop_registration_nowait(self, local_id: int, reaped: bool = False) -> None:
        """Non-awaiting unsubscribe body — loop-affine and therefore atomic.

        The vanished-consumer finalizer hops here via
        ``call_soon_threadsafe``; never awaits, so it needs no lock (the same
        cooperative-atomicity argument as ``_dispatch``)."""
        reg = self._registrations.pop(local_id, None)
        if reg is not None:
            if reaped:
                logger.warning(
                    "niwaki: a subscription was garbage-collected without close() — "
                    "dropping its registration (call close() or use the context manager)"
                )
            self._wire_to_local.pop(reg.wire_id, None)
            reg.queue.put_nowait(_STOP)

    async def unsubscribe(self, local_id: int) -> None:
        """Drop local bookkeeping for one subscription (see :meth:`AsyncRawSubscription.close`)."""
        async with self._state_lock:
            reg = self._registrations.pop(local_id, None)
            if reg is not None:
                self._wire_to_local.pop(reg.wire_id, None)
        if reg is not None:
            reg.queue.put_nowait(_STOP)

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """Snapshot every subscription currently tracked on this socket.

        See :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.list_subscriptions`.
        Lock-free by design, like :meth:`_wire_id_for` — a non-awaiting read
        cannot interleave with a mutation under asyncio's cooperative
        scheduling.

        Returns:
            One :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`
            per tracked subscription, in no particular order.
        """
        return [
            self._subscription_info(local_id, reg) for local_id, reg in self._registrations.items()
        ]

    async def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        See the sync
        :class:`~niwaki.transport._subscription_socket.SubscriptionSocket`'s
        method of the same name for the escalation-safety semantics.
        """
        async with self._state_lock:
            snapshot = list(self._registrations.items())
        for local_id, reg in snapshot:
            await self._refresh_no_escalation(local_id, reg)
        return self.list_subscriptions()

    async def refresh_one_now(self, local_id: int) -> SubscriptionInfo:
        """Force an immediate refresh of one subscription. See
        :meth:`AsyncRawSubscription.refresh_now` — this is its implementation.

        Raises:
            SubscriptionError: This subscription is no longer tracked (closed).
        """
        reg = self._registrations.get(local_id)  # lock-free: see _wire_id_for
        if reg is None:
            raise exceptions.SubscriptionError(
                "cannot refresh: this subscription has already been closed"
            )
        await self._refresh_no_escalation(local_id, reg)
        info = self._subscription_info_for(local_id)
        if info is None:
            raise exceptions.SubscriptionError(
                "cannot refresh: this subscription was closed during the refresh"
            )
        return info

    async def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the socket itself stays open.

        See the sync
        :class:`~niwaki.transport._subscription_socket.SubscriptionSocket`'s
        method of the same name.
        """
        async with self._state_lock:
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
        for reg in regs:
            reg.queue.put_nowait(_STOP)

    async def aclose(self) -> None:
        """Tear down the socket and every tracked subscription.

        Called by :meth:`~niwaki.transport.session_async.AsyncApicSession.close`.
        Every blocked :class:`AsyncRawSubscription` iterator wakes with a
        plain ``StopAsyncIteration`` rather than hanging. Unlike the sync
        ``close()``, this can (and does) properly cancel and await its
        background tasks — a coroutine context affords a graceful teardown
        the sync side cannot.
        """
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._handle.closed = True
            ws = self._ws
            self._ws = None
            self._handle.sock = None
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
            reader_task = self._reader_task
            refresh_task = self._refresh_task
        for reg in regs:
            reg.queue.put_nowait(_STOP)
        for task in (reader_task, refresh_task):
            if task is not None:
                task.cancel()
        for task in (reader_task, refresh_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _wire_id_for(self, local_id: int) -> str:
        # Lock-free by design: observability-only, and a plain dict .get() is
        # atomic within asyncio's cooperative scheduling (no await inside it,
        # so no other coroutine can interleave mid-read).
        reg = self._registrations.get(local_id)
        return reg.wire_id if reg is not None else ""

    @staticmethod
    def _subscription_info(local_id: int, reg: _AsyncRegistration) -> SubscriptionInfo:
        """Build a :class:`SubscriptionInfo` snapshot."""
        return SubscriptionInfo(
            local_id=local_id,
            subscription_id=reg.wire_id,
            path=reg.path,
            params=dict(reg.params),
            refresh_timeout=reg.refresh_timeout,
            consecutive_refresh_failures=reg.consecutive_refresh_failures,
            seconds_until_refresh=max(0.0, reg.next_refresh_at - time.monotonic()),
            pending=reg.queue.qsize(),
            dropped=reg.dropped_total,
        )

    def _subscription_info_for(self, local_id: int) -> SubscriptionInfo | None:
        # Lock-free by design: see _wire_id_for.
        reg = self._registrations.get(local_id)
        return self._subscription_info(local_id, reg) if reg is not None else None

    # ── Internal: the REST half of subscribe/refresh ──────────────────────────

    async def _do_subscribe(
        self, path: str, params: dict[str, str], refresh_timeout: int | None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Issue the ``subscription=yes`` GET and return ``(wire_id, initial_imdata)``."""
        full_params: dict[str, str] = {**params, "subscription": "yes"}
        if refresh_timeout is not None:
            full_params["refresh-timeout"] = str(refresh_timeout)
        try:
            resp = await self._session._request_checked(path, full_params)
        except exceptions.APIError as exc:
            raise exceptions.SubscribeRejectedError(
                exc.status_code, exc.apic_message, apic_code=exc.apic_code
            ) from exc
        body: dict[str, Any] = resp.json()
        wire_id = body.get("subscriptionId")
        if not wire_id:
            raise exceptions.SubscribeRejectedError(
                resp.status_code, "APIC response carried no subscriptionId"
            )
        return str(wire_id), list(body.get("imdata", []))

    # ── Internal: socket lifecycle ─────────────────────────────────────────────

    async def _connect(self) -> Any:
        """Open a fresh WebSocket connection. No side effects on ``self``.

        Deliberately does not touch ``self._ws``/``self._handle`` — the
        caller decides, under the lock, whether to adopt or discard the
        result (see :meth:`_ensure_open`/:meth:`_reconnect_and_resubscribe_all`).
        """
        await self._session._ensure_token()  # fresh token before it goes in the URL
        token = self._session._token_state.token  # type: ignore[union-attr]
        parsed = urlsplit(self._session._host)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{scheme}://{parsed.netloc}/socket{token}"
        import websockets.asyncio.client as ws_client  # lazy: protects the cold-start budget

        return await ws_client.connect(url, ssl=self._session._ws_ssl_context)

    async def _ensure_open(self) -> None:
        async with self._state_lock:
            if self._closed:
                raise exceptions.SubscriptionError(
                    "cannot subscribe: the subscription socket has been closed"
                )
            if self._ws is not None:
                return
        # Connect *outside* the lock: a cooperative asyncio.Lock held across a
        # slow connect() would stall every other coroutine waiting on this
        # socket, unlike the sync side's dedicated thread holding its own
        # threading.Lock across a blocking connect.
        ws = await self._connect()
        async with self._state_lock:
            if self._closed or self._ws is not None:
                # aclose() won the race, or another coroutine already opened
                # one while we were connecting — our connection is redundant.
                with contextlib.suppress(Exception):
                    await ws.close()
                if self._closed:
                    # Raise rather than return: proceeding would REST-subscribe
                    # and register on a closed socket, leaving the consumer's
                    # queue without a _STOP forever.
                    raise exceptions.SubscriptionError(
                        "cannot subscribe: the subscription socket has been closed"
                    )
                return
            self._ws = ws
            self._handle.sock = ws.transport.get_extra_info("socket")
            self._dead = False
            # A fresh socket means a fresh task generation — any task from a
            # previous life of this socket sees the bump and exits (see the
            # sync twin's _ensure_open).
            self._generation += 1
            ref: weakref.ReferenceType[AsyncSubscriptionSocket] = weakref.ref(self)
            self._reader_task = asyncio.create_task(_reader_entry(ref, self._generation))
            self._refresh_task = asyncio.create_task(_refresh_entry(ref, self._generation))

    # ── Internal: dispatch (called from the free-function reader loop) ───────

    def _dispatch(self, raw: str | bytes) -> None:
        # No lock: this method never awaits, so asyncio's cooperative
        # scheduling makes it atomic with respect to every other coroutine on
        # this loop — nothing can mutate _registrations/_wire_to_local mid-call.
        #
        # The guard spans the WHOLE frame handling, not just json.loads —
        # see the sync twin's _dispatch: a valid-JSON non-object frame or a
        # malformed imdata shape must be skipped, never kill the reader.
        # Only the frame's length is logged, never its content.
        try:
            data: Any = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("subscription push frame is not a JSON object")
            wire_ids_raw = data.get("subscriptionId") or []
            wire_ids = [wire_ids_raw] if isinstance(wire_ids_raw, str) else list(wire_ids_raw)
            imdata = data.get("imdata") or []

            targets = [
                self._registrations[local_id]
                for wire_id in wire_ids
                if (local_id := self._wire_to_local.get(wire_id)) is not None
                and local_id in self._registrations
            ]
            if not targets:
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
                for reg in targets:
                    self._offer(reg, event)
        except Exception:  # a malformed frame must never kill the shared reader
            logger.warning(
                "niwaki: malformed subscription push frame ignored (length=%d)", len(raw)
            )

    @staticmethod
    def _offer(reg: _AsyncRegistration, event: RawSubscriptionEvent) -> None:
        """Enqueue a data event under the backpressure bound — mirror of the
        sync :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket._offer`."""
        if reg.queue.qsize() >= reg.max_pending:
            reg.dropped_total += 1
            if not reg.overflow_open:
                reg.overflow_open = True
                reg.queue.put_nowait(SubscriptionOverflow(dropped_total=reg.dropped_total))
                logger.warning(
                    "niwaki: subscription buffer full (max_pending=%d) — "
                    "dropping incoming events until the consumer catches up",
                    reg.max_pending,
                )
            return
        if reg.overflow_open and reg.queue.qsize() <= reg.max_pending // 2:
            # Hysteresis — mirror of the sync _offer: one marker per episode.
            reg.overflow_open = False
        reg.queue.put_nowait(event)

    # ── Internal: refresh sweep (entry point is the free-function loop) ──────

    async def _do_refresh(self, reg: _AsyncRegistration) -> bool:
        """Issue the ``subscriptionRefresh`` GET. No bookkeeping — callers own that.

        Catches every :class:`~niwaki.exceptions.NiwakiError`, not just
        ``APIError`` — see the sync twin's ``_do_refresh``: a transport/auth
        error from an unreachable APIC must count as a failed refresh attempt
        feeding the escalation machinery, never kill the sweep task.
        """
        try:
            await self._session._request_checked(
                "/api/subscriptionRefresh.json", {"id": reg.wire_id}
            )
        except exceptions.NiwakiError as exc:
            logger.warning(
                "niwaki: subscription refresh failed (%s, path=%s, id=%s)",
                type(exc).__name__,
                reg.path,
                reg.wire_id,
            )
            return False
        return True

    async def _refresh_no_escalation(self, local_id: int, reg: _AsyncRegistration) -> bool:
        """A manual refresh (bulk or single) — never escalates. See
        :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket._refresh_no_escalation`.
        """
        ok = await self._do_refresh(reg)
        async with self._state_lock:
            if ok and local_id in self._registrations:
                reg.consecutive_refresh_failures = 0
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
        return ok

    async def _refresh_one(self, local_id: int, reg: _AsyncRegistration) -> None:
        """The scheduled refresh path — the only one that can escalate. See
        :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket._refresh_one`.
        """
        ok = await self._do_refresh(reg)
        still_tracked = False
        failures = 0
        async with self._state_lock:
            if local_id in self._registrations:
                still_tracked = True
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
                reg.consecutive_refresh_failures = 0 if ok else reg.consecutive_refresh_failures + 1
                failures = reg.consecutive_refresh_failures
        if not still_tracked or ok:
            return
        if failures >= _REFRESH_FAILURE_ESCALATION_THRESHOLD:
            await self._escalate(local_id, reg)
        else:
            reg.queue.put_nowait(
                SubscriptionRefreshFailed(
                    subscription_id=reg.wire_id, consecutive_failures=failures
                )
            )

    async def _escalate(self, local_id: int, reg: _AsyncRegistration) -> None:
        """Force a per-subscription resubscribe after consecutive missed refreshes.

        See :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket._escalate`
        — identical semantics, ``_do_subscribe``/lock acquisition awaited.
        """
        escalated_at = time.time()
        old_wire = reg.wire_id
        try:
            new_wire, _initial = await self._do_subscribe(reg.path, reg.params, reg.refresh_timeout)
        except exceptions.NiwakiError as exc:
            # NiwakiError, not just SubscriptionError — see the sync twin:
            # transport/auth errors from an unreachable APIC must not escape
            # and kill the refresh sweep for every subscription.
            async with self._state_lock:
                if local_id not in self._registrations or reg.wire_id != old_wire:
                    return
                self._wire_to_local.pop(old_wire, None)
                self._registrations.pop(local_id, None)
            reg.queue.put_nowait(
                _Fatal(
                    exceptions.SubscriptionLostError(
                        f"subscription refresh failed {_REFRESH_FAILURE_ESCALATION_THRESHOLD} "
                        f"times consecutively and the recovery resubscribe also failed: {exc}",
                        reason=exceptions.SubscriptionLostReason.REFRESH_ESCALATION,
                    )
                )
            )
            return
        async with self._state_lock:
            if local_id not in self._registrations:
                return
            if reg.wire_id != old_wire:
                return
            self._wire_to_local.pop(old_wire, None)
            self._wire_to_local[new_wire] = local_id
            reg.wire_id = new_wire
            reg.consecutive_refresh_failures = 0
            reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
        reg.queue.put_nowait(
            SubscriptionGap(
                disconnected_at=escalated_at,
                reconnected_at=time.time(),
                old_subscription_id=old_wire,
                new_subscription_id=new_wire,
            )
        )

    # ── Internal: reconnect ─────────────────────────────────────────────────────

    async def _reconnect_and_resubscribe_all(self) -> bool:
        """Reconnect the socket and resubscribe every tracked query from scratch.

        See the sync
        :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket._reconnect_and_resubscribe_all`
        for the full semantics. Uses the ``@stamina.retry`` decorator rather
        than ``stamina.retry_context`` — the latter is sync-only (its
        ``Attempt`` has no ``__aenter__`` and its inter-attempt sleep blocks),
        exactly like this project's own async session already does for its
        own retried requests.

        Returns:
            ``True`` if the socket itself came back. ``False`` if reconnect
            could not be completed at all.
        """
        import websockets.exceptions as ws_exceptions

        logger.warning("niwaki: subscription websocket disconnected — reconnecting")
        disconnected_at = time.time()

        # NiwakiError is in the retry net alongside the websocket/OS errors:
        # _connect performs HTTP via _ensure_token, which can raise
        # TokenRefreshError/ConnectionError/… — same reasoning as the sync
        # twin's _reconnect_and_resubscribe_all.
        @stamina.retry(
            on=(ws_exceptions.WebSocketException, OSError, exceptions.NiwakiError),
            attempts=_RECONNECT_ATTEMPTS,
            wait_initial=_RECONNECT_WAIT_INITIAL,
            wait_max=_RECONNECT_WAIT_MAX,
            wait_jitter=_RECONNECT_WAIT_JITTER,
        )
        async def _attempt_connect() -> Any:
            return await self._connect()

        try:
            ws = await _attempt_connect()
        except (ws_exceptions.WebSocketException, OSError, exceptions.NiwakiError) as exc:
            async with self._state_lock:
                if self._closed:
                    return False
            await self._fail_all(
                exceptions.SubscriptionLostError(
                    f"subscription websocket lost, reconnect exhausted: {exc}",
                    reason=exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED,
                )
            )
            return False

        async with self._state_lock:
            if self._closed:
                # close() raced with a reconnect that just succeeded — the
                # fresh socket is not needed; close it and stop, exactly as
                # close() itself would have, had it won the race outright.
                with contextlib.suppress(Exception):
                    await ws.close()
                return False
            self._ws = ws
            self._handle.sock = ws.transport.get_extra_info("socket")
            snapshot = dict(self._registrations)
        reconnected_at = time.time()
        for local_id, reg in snapshot.items():
            old_wire = reg.wire_id
            try:
                # NiwakiError, not just SubscriptionError — see _escalate.
                new_wire, _initial = await self._do_subscribe(
                    reg.path, reg.params, reg.refresh_timeout
                )
            except exceptions.NiwakiError as exc:
                async with self._state_lock:
                    if local_id not in self._registrations or reg.wire_id != old_wire:
                        # A concurrent _escalate already re-registered this
                        # subscription under a fresh wire id (or unsubscribe
                        # removed it) — its fate is no longer this loop's call.
                        continue
                    self._wire_to_local.pop(old_wire, None)
                    self._registrations.pop(local_id, None)
                reg.queue.put_nowait(
                    _Fatal(
                        exceptions.SubscriptionLostError(
                            f"could not resubscribe after reconnect: {exc}",
                            reason=exceptions.SubscriptionLostReason.RESUBSCRIBE_FAILED,
                        )
                    )
                )
                continue
            async with self._state_lock:
                if local_id not in self._registrations or reg.wire_id != old_wire:
                    # Same race on the success path — mirror of the guard in
                    # _escalate: adopting now would leave the escalated wire
                    # id mapped alongside ours forever, delivering every
                    # event carrying it twice. Drop our redundant subscribe;
                    # its unused wire id lapses server-side at refresh-timeout.
                    continue
                self._wire_to_local.pop(old_wire, None)
                self._wire_to_local[new_wire] = local_id
                reg.wire_id = new_wire
                reg.next_refresh_at = time.monotonic() + _refresh_interval(reg.refresh_timeout)
            reg.queue.put_nowait(
                SubscriptionGap(
                    disconnected_at=disconnected_at,
                    reconnected_at=reconnected_at,
                    old_subscription_id=old_wire,
                    new_subscription_id=new_wire,
                )
            )
        return True

    async def _fail_all(self, exc: exceptions.SubscriptionLostError) -> None:
        async with self._state_lock:
            regs = list(self._registrations.values())
            self._registrations.clear()
            self._wire_to_local.clear()
            ws = self._ws
            self._ws = None
            # Not handle.closed = True: a fresh subscribe() later must still
            # be able to reopen (see _ensure_open), and the finalizer should
            # cover that new socket too if it is ever abandoned in turn.
            self._handle.sock = None
            self._dead = True
            # Invalidate the current reader/refresh pair — see the sync twin.
            self._generation += 1
        for reg in regs:
            reg.queue.put_nowait(_Fatal(exc))
        if ws is not None:
            # Best effort — the socket may already be dead; closing it wakes
            # a reader still parked at recv() so it can observe the
            # generation bump and exit instead of hanging on a zombie.
            with contextlib.suppress(Exception):
                await ws.close()


# ── Task entry points (free coroutine functions — weak reference only) ───────
#
# See the module docstring: these hold a weakref.ref, not `self`, resolved
# fresh on each loop iteration and never across an `await`. A bound-method
# coroutine (self._reader_loop()) would instead keep the AsyncSubscriptionSocket
# permanently reachable for as long as the task runs — almost always, since
# the reader is parked at `await recv()` most of the time — defeating the
# weakref.finalize safety net exactly as the sync bound-method bug did.


async def _reader_entry(
    ref: weakref.ReferenceType[AsyncSubscriptionSocket], generation: int
) -> None:
    """Reader-task entry point. See the module note above on why this is a
    free function taking a weak reference, not a bound method.

    The try/except is the **last line of defense** — see the sync twin's
    ``_reader_entry``: a silently dead reader (consumers parked forever on
    queues nothing feeds) is the one outcome that must be impossible.
    ``asyncio.CancelledError`` inherits ``BaseException`` and passes through
    untouched, so :meth:`AsyncSubscriptionSocket.aclose`'s cancellation is
    unaffected.
    """
    try:
        await _reader_loop(ref, generation)
    except Exception as exc:
        await _fail_from_crash(ref, "reader", exc)


async def _reader_loop(
    ref: weakref.ReferenceType[AsyncSubscriptionSocket], generation: int
) -> None:
    """The reader loop body — recv/demux/reconnect until closed or superseded."""
    import websockets.exceptions as ws_exceptions

    while True:
        socket = ref()
        if socket is None:
            return
        async with socket._state_lock:
            if socket._closed or socket._generation != generation:
                return
            ws = socket._ws
        del socket  # never hold a strong reference across the blocking recv()
        try:
            raw = await ws.recv()
        except (ws_exceptions.WebSocketException, OSError):
            socket = ref()
            if socket is None:
                return
            async with socket._state_lock:
                if socket._closed or socket._generation != generation:
                    return
            if not await socket._reconnect_and_resubscribe_all():
                return
            del socket
            continue
        socket = ref()
        if socket is None:
            return
        socket._dispatch(raw)
        del socket


async def _refresh_entry(
    ref: weakref.ReferenceType[AsyncSubscriptionSocket], generation: int
) -> None:
    """Refresh-sweep task entry point. See the module note above on why
    this is a free function taking a weak reference, not a bound method —
    and :func:`_reader_entry` on the last-line-of-defense guard."""
    try:
        await _refresh_loop(ref, generation)
    except Exception as exc:
        await _fail_from_crash(ref, "refresh", exc)


async def _refresh_loop(
    ref: weakref.ReferenceType[AsyncSubscriptionSocket], generation: int
) -> None:
    """The sweep loop body — refresh due registrations until closed or superseded."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        socket = ref()
        if socket is None:
            return
        async with socket._state_lock:
            if socket._closed or socket._dead or socket._generation != generation:
                return
            now = time.monotonic()
            due = [
                (local_id, reg)
                for local_id, reg in socket._registrations.items()
                if reg.next_refresh_at <= now
            ]
        for local_id, reg in due:
            await socket._refresh_one(local_id, reg)
        del socket


async def _fail_from_crash(
    ref: weakref.ReferenceType[AsyncSubscriptionSocket], role: str, exc: Exception
) -> None:
    """Last line of defense for a background task that died unexpectedly.

    Mirror of the sync :func:`~niwaki.transport._subscription_socket._fail_from_crash`:
    every consumer wakes with ``SubscriptionLostError`` (``reason=INTERNAL_ERROR``)
    instead of blocking forever, and the socket is left restartable by a
    later :meth:`AsyncSubscriptionSocket.subscribe`. Only the exception's
    class name is logged, never its message — an exception raised while
    handling a frame can embed frame content, and payloads are never logged.
    """
    logger.error(
        "niwaki: subscription %s task died on an unexpected %s — every tracked "
        "subscription is failed with SubscriptionLostError; a later subscribe() "
        "reopens the socket",
        role,
        type(exc).__name__,
    )
    socket = ref()
    if socket is None:
        return
    await socket._fail_all(
        exceptions.SubscriptionLostError(
            f"subscription {role} task crashed unexpectedly ({type(exc).__name__})",
            reason=exceptions.SubscriptionLostReason.INTERNAL_ERROR,
        )
    )
