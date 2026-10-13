"""Same-client order hints with exclusive leases for bounded reads and writes.

The listener never binds manual orders or sends orders. A borrowed connection
suspends it before another adapter connects with the same client ID.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Any, cast

from .connection import validate_paper_connection
from .ibkr_probe import (
    _INFORMATIONAL_ERROR_CODES,
    _load_ibapi,
    _parse_error_arguments,
)


class _ClientSlot:
    def __init__(self) -> None:
        self.lock = RLock()
        self.listener: OrderObserver | None = None
        self.depth = 0


_slots: dict[tuple[str, int, int], _ClientSlot] = {}
_slots_lock = RLock()


def _slot(host: str, port: int, client_id: int) -> _ClientSlot:
    with _slots_lock:
        return _slots.setdefault((host, port, client_id), _ClientSlot())


@contextmanager
def borrow_client(host: str, port: int, client_id: int) -> Iterator[None]:
    """Serialize adapters and release the listener socket before their connect."""
    slot = _slot(host, port, client_id)
    with slot.lock:
        listener = slot.listener
        if slot.depth == 0 and listener is not None:
            listener._pause()
        slot.depth += 1
        try:
            yield
        finally:
            slot.depth -= 1
            if slot.depth == 0 and slot.listener is listener and listener is not None:
                listener._resume()


def leased_client[F: Callable[..., Any]](function: F) -> F:
    """Apply the same lease to capture requests and paper transport methods."""
    parameters = signature(function)

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        values = parameters.bind(*args, **kwargs).arguments
        request = values.get("request")
        host, port, client_id = (
            (request.host, request.port, request.client_id)
            if request is not None
            else (values["host"], values["port"], values["client_id"])
        )
        with borrow_client(host, port, client_id):
            return function(*args, **kwargs)

    return cast(F, wrapped)


class OrderObserver:
    """Persistent submitting-client listener; callbacks are only refresh hints."""

    def __init__(
        self,
        account: str,
        port: int,
        client_id: int,
        timeout_seconds: float,
        on_change: Callable[[], None],
        on_health: Callable[[str], None],
    ) -> None:
        validate_paper_connection(
            host="127.0.0.1",
            port=port,
            client_id=client_id,
            expected_account=account,
            timeout_seconds=timeout_seconds,
        )
        self.account, self.port, self.client_id = account, port, client_id
        self.timeout = timeout_seconds
        self._on_change, self._on_health = on_change, on_health
        self._slot = _slot("127.0.0.1", port, client_id)
        self._generation = 0
        self._stop = Event()
        self._thread: Thread | None = None
        self._app: Any = None
        self._reader: Thread | None = None
        self._baseline: dict[int, tuple[Any, ...]] | None = None
        self._statuses: dict[int, tuple[Any, ...]] = {}

    def start(self) -> None:
        with self._slot.lock:
            if self._slot.listener not in (None, self):
                raise RuntimeError("this client ID already has an order listener")
            self._slot.listener = self
            self._pause()
            self._resume()

    def stop(self) -> None:
        with self._slot.lock:
            if self._slot.listener is self:
                self._slot.listener = None
            self._pause()

    def _pause(self) -> None:
        self._generation += 1
        self._stop.set()
        app, thread = self._app, self._thread
        if app is not None:
            app.disconnect()
        if thread is not None and thread is not current_thread():
            thread.join(timeout=self.timeout)
            if thread.is_alive():
                raise RuntimeError("order listener did not release its client ID")
        if self._reader is not None and self._reader.is_alive():
            raise RuntimeError("order listener reader did not release its client ID")
        if app is not None and app.isConnected():
            raise RuntimeError("order listener socket is still connected")
        self._app = None
        self._thread = None
        self._reader = None

    def _resume(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._generation += 1
        generation = self._generation
        stop = self._stop = Event()
        self._thread = Thread(
            target=self._run,
            args=(generation, stop),
            name="ibkr-order-observer",
            daemon=True,
        )
        self._thread.start()

    def _run(self, generation: int, stop: Event) -> None:
        def current() -> bool:
            return generation == self._generation and not stop.is_set()

        app: Any = None
        reader: Thread | None = None
        try:
            imports = _load_ibapi()
            owner = self

            class App(imports.EWrapper, imports.EClient):  # type: ignore[misc, name-defined]
                def __init__(self) -> None:
                    imports.EWrapper.__init__(self)
                    imports.EClient.__init__(self, self)
                    self.ready = Event()
                    self.initial = True
                    self.orders: dict[int, tuple[Any, ...]] = {}
                    self.identities: set[int] = set()
                    self.statuses = owner._statuses

                def nextValidId(self, orderId: int) -> None:
                    del orderId
                    self.ready.set()

                def openOrder(
                    self, orderId: int, contract: Any, order: Any, state: Any
                ) -> None:
                    if (
                        not current()
                        or str(getattr(order, "account", "")) != owner.account
                    ):
                        return
                    perm_id = int(getattr(order, "permId", 0) or 0)
                    if perm_id <= 0:
                        return
                    self.identities.add(perm_id)
                    material = (
                        int(getattr(contract, "conId", 0)),
                        orderId,
                        str(getattr(order, "orderType", "")),
                        str(getattr(order, "totalQuantity", "")),
                        str(getattr(order, "auxPrice", "")),
                        str(getattr(order, "lmtPrice", "")),
                        str(getattr(order, "trailingPercent", "")),
                        str(getattr(order, "lmtPriceOffset", "")),
                        str(getattr(order, "trailStopPrice", "")),
                        str(getattr(order, "action", "")),
                        str(getattr(order, "tif", "")),
                        str(getattr(order, "ocaGroup", "")),
                        str(getattr(order, "parentId", "")),
                        str(getattr(state, "status", "")),
                    )
                    prior = self.orders.get(perm_id)
                    self.orders[perm_id] = material
                    if not self.initial and material != prior and current():
                        owner._on_change()

                def openOrderEnd(self) -> None:
                    if not current() or not self.initial:
                        return
                    self.initial = False
                    previous = owner._baseline
                    owner._baseline = dict(self.orders)
                    owner._on_health("connected")
                    if previous is not None and previous != self.orders:
                        owner._on_change()

                def orderStatus(
                    self,
                    orderId: int,
                    status: str,
                    filled: Any,
                    remaining: Any,
                    avgFillPrice: Any,
                    permId: int,
                    *args: Any,
                ) -> None:
                    del avgFillPrice, args
                    if not current():
                        return
                    material = (orderId, status, str(filled), str(remaining))
                    prior = self.statuses.get(permId)
                    self.statuses[permId] = material
                    if (
                        permId in self.identities
                        and not self.initial
                        and material != prior
                        and current()
                    ):
                        owner._on_change()

                def connectionClosed(self) -> None:
                    if current():
                        owner._on_health("disconnected")

                def error(self, reqId: int, *args: Any) -> None:
                    code, _ = _parse_error_arguments(args)
                    if code in {201, 202} and current():
                        owner._on_change()
                        return
                    del reqId
                    if code not in _INFORMATIONAL_ERROR_CODES and current():
                        owner._on_health("error")
                        stop.set()

            app = App()
            # A borrow may begin before this worker starts. Do not hold the
            # lease lock while waiting for callbacks or shutting down.
            while not stop.is_set():
                if self._slot.lock.acquire(timeout=0.05):
                    try:
                        if not current():
                            return
                        self._app = app
                        app.connect("127.0.0.1", self.port, self.client_id)
                    finally:
                        self._slot.lock.release()
                    break
            if not current():
                return
            reader = Thread(target=app.run, name="ibkr-order-reader", daemon=True)
            self._reader = reader
            reader.start()
            deadline = monotonic() + self.timeout
            while not app.ready.wait(0.05):
                if not current():
                    return
                if monotonic() >= deadline:
                    self._on_health("error")
                    return
            app.reqOpenOrders()  # nonzero own-client request: never binds manual orders
            retry_at = monotonic() + 15
            while not stop.wait(0.1):
                if app.initial and monotonic() >= deadline:
                    self._on_health("error")
                    return
                if not app.isConnected() or not reader.is_alive():
                    if current():
                        self._on_health("disconnected")
                    return
                if monotonic() >= retry_at:
                    self._on_change()  # bounded recovery even if a callback was missed
                    retry_at = monotonic() + 15
        except Exception:
            if current():
                self._on_health("error")
        finally:
            if app is not None:
                app.disconnect()
            if reader is not None and reader is not current_thread():
                reader.join(timeout=self.timeout)
