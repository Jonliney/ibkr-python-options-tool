"""Position change hints from a persistent, observation-only TWS connection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any

from .broker.ibkr import _INFORMATIONAL_ERROR_CODES
from .connection import validate_paper_connection
from .ibkr_probe import _load_ibapi, _parse_error_arguments


@dataclass(frozen=True, slots=True)
class ObservationSettings:
    account: str
    port: int
    client_id: int
    capture_client_id: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        validate_paper_connection(
            host="127.0.0.1",
            port=self.port,
            client_id=self.client_id,
            expected_account=self.account,
            timeout_seconds=self.timeout_seconds,
        )
        if self.client_id == self.capture_client_id:
            raise ValueError("observer and capture client IDs must differ")


class PositionObserver:
    """Own one TWS subscription; callbacks carry hints, never verified state.

    A generation counter rejects callbacks from a prior socket after settings
    change or reconnect. The callback thread does not run broker captures.
    """

    def __init__(
        self,
        on_change: Callable[[int], None],
        on_health: Callable[[int, str], None],
    ) -> None:
        self._on_change = on_change
        self._on_health = on_health
        self._lock = Lock()
        self._generation = 0
        self._start_count = 0
        self._stop = Event()
        self._thread: Thread | None = None
        self._app: Any = None

    def start(
        self,
        settings: ObservationSettings,
        on_generation: Callable[[int], None] | None = None,
    ) -> int:
        self.stop()
        with self._lock:
            reconnecting = self._start_count > 0
            self._start_count += 1
            self._generation += 1
            generation = self._generation
            self._stop = Event()
            self._thread = Thread(
                target=self._run,
                args=(settings, generation, reconnecting, self._stop),
                name="ibkr-position-observer",
                daemon=True,
            )
            thread = self._thread
        if on_generation is not None:
            on_generation(generation)
        thread.start()
        return generation

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            stop = self._stop
            app = self._app
            thread = self._thread
            self._app = None
            self._thread = None
        stop.set()
        if app is not None:
            try:
                app.cancelPositions()
            except Exception:
                pass
            finally:
                app.disconnect()
        if thread is not None and thread is not current_thread():
            thread.join(timeout=1)

    def _current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _run(
        self,
        settings: ObservationSettings,
        generation: int,
        reconnecting: bool,
        stop: Event,
    ) -> None:
        try:
            imports = _load_ibapi()
            owner = self

            class App(imports.EWrapper, imports.EClient):  # type: ignore[misc, name-defined]
                def __init__(self) -> None:
                    imports.EWrapper.__init__(self)
                    imports.EClient.__init__(self, self)
                    self.handshake = Event()
                    self.baseline = False
                    self.baseline_has_options = False
                    self.observed_options: dict[int, tuple[str, str]] = {}
                    self.fatal = False

                def nextValidId(self, orderId: int) -> None:
                    del orderId
                    self.handshake.set()

                def position(
                    self, account: str, contract: Any, pos: Any, avgCost: Any
                ) -> None:
                    if (
                        account != settings.account
                        or str(getattr(contract, "secType", "")) != "OPT"
                    ):
                        return
                    contract_id = int(getattr(contract, "conId", 0))
                    observed = (str(pos), str(avgCost))
                    prior = self.observed_options.get(contract_id)
                    self.observed_options[contract_id] = observed
                    if not self.baseline:
                        self.baseline_has_options = True
                    elif observed != prior and owner._current(generation):
                        owner._on_change(generation)

                def positionEnd(self) -> None:
                    if self.fatal or self.baseline:
                        return
                    self.baseline = True
                    if owner._current(generation):
                        owner._on_health(generation, "connected")
                        # The launch capture already verified an empty portfolio.
                        # Reconcile a populated baseline, or any reconnect that
                        # may have missed a removal while offline.
                        if self.baseline_has_options or reconnecting:
                            owner._on_change(generation)

                def connectionClosed(self) -> None:
                    if owner._current(generation):
                        owner._on_health(generation, "disconnected")

                def error(self, reqId: int, *args: Any) -> None:
                    del reqId
                    code, _message = _parse_error_arguments(args)
                    if code not in _INFORMATIONAL_ERROR_CODES and owner._current(
                        generation
                    ):
                        self.fatal = True
                        owner._on_health(
                            generation,
                            "client-id-in-use" if code == 326 else "error",
                        )

            app = App()
            with self._lock:
                if generation != self._generation:
                    return
                self._app = app
            app.connect("127.0.0.1", settings.port, settings.client_id)
            reader = Thread(target=app.run, name="ibkr-position-reader", daemon=True)
            reader.start()
            if not app.handshake.wait(settings.timeout_seconds):
                if self._current(generation):
                    self._on_health(generation, "error")
                return
            app.reqPositions()
            deadline = monotonic() + settings.timeout_seconds
            while not stop.wait(0.2):
                if app.fatal:
                    return
                if not app.isConnected() or not reader.is_alive():
                    if self._current(generation):
                        self._on_health(generation, "disconnected")
                    return
                if not app.baseline and monotonic() >= deadline:
                    if self._current(generation):
                        self._on_health(generation, "error")
                    return
        except Exception:
            if self._current(generation):
                self._on_health(generation, "error")
        finally:
            try:
                if "app" in locals() and app.isConnected():
                    try:
                        app.cancelPositions()
                    finally:
                        app.disconnect()
            finally:
                with self._lock:
                    if generation == self._generation:
                        self._app = None


__all__ = ["ObservationSettings", "PositionObserver"]
