from threading import Event
from types import SimpleNamespace

import pytest

from ibkr_options_manager import observation
from ibkr_options_manager.observation import ObservationSettings, PositionObserver


def test_observer_requires_a_distinct_nonzero_paper_client_id() -> None:
    with pytest.raises(ValueError, match="must differ"):
        ObservationSettings(
            account="DU1234567",
            port=7497,
            client_id=17,
            capture_client_id=17,
            timeout_seconds=5,
        )
    with pytest.raises(ValueError, match="client_id"):
        ObservationSettings(
            account="DU1234567",
            port=7497,
            client_id=0,
            capture_client_id=17,
            timeout_seconds=5,
        )


def test_position_subscription_uses_initial_inventory_as_a_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = Event()
    changes: list[int] = []
    health: list[str] = []

    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper: object) -> None:
            self.wrapper = wrapper
            self.active = False
            self.cancelled = False

        def connect(self, host: str, port: int, client_id: int) -> None:
            assert (host, port, client_id) == ("127.0.0.1", 7497, 18)
            self.active = True

        def run(self) -> None:
            self.wrapper.nextValidId(1)  # type: ignore[attr-defined]
            connected.wait(2)

        def isConnected(self) -> bool:
            return self.active

        def reqPositions(self) -> None:
            contract = SimpleNamespace(secType="OPT")
            self.wrapper.position("DU1234567", contract, 1, 100)  # type: ignore[attr-defined]
            assert changes == []
            self.wrapper.positionEnd()  # type: ignore[attr-defined]
            self.wrapper.position("DU1234567", contract, 2, 100)  # type: ignore[attr-defined]
            self.wrapper.position("DU1234567", contract, 2, 100)  # type: ignore[attr-defined]

        def cancelPositions(self) -> None:
            self.cancelled = True

        def disconnect(self) -> None:
            self.active = False
            connected.set()

    monkeypatch.setattr(
        observation,
        "_load_ibapi",
        lambda: SimpleNamespace(EWrapper=Wrapper, EClient=Client),
    )
    observer = PositionObserver(
        changes.append, lambda _generation, value: health.append(value)
    )
    settings = ObservationSettings("DU1234567", 7497, 18, 17, 2)
    observer.start(settings)
    try:
        for _ in range(100):
            if len(changes) == 2:
                break
            Event().wait(0.01)
        assert len(changes) == 2
        assert changes[0] == changes[1]
        assert health == ["connected"]
    finally:
        observer.stop()


def test_empty_initial_position_baseline_does_not_trigger_repeat_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = Event()
    changes: list[int] = []
    health: list[str] = []

    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper: object) -> None:
            self.wrapper = wrapper
            self.active = False

        def connect(self, *_args: object) -> None:
            self.active = True

        def run(self) -> None:
            self.wrapper.nextValidId(1)  # type: ignore[attr-defined]
            connected.wait(2)

        def isConnected(self) -> bool:
            return self.active

        def reqPositions(self) -> None:
            self.wrapper.positionEnd()  # type: ignore[attr-defined]
            self.wrapper.positionEnd()  # type: ignore[attr-defined]

        def cancelPositions(self) -> None:
            pass

        def disconnect(self) -> None:
            self.active = False
            connected.set()

    monkeypatch.setattr(observation, "_load_ibapi",
                        lambda: SimpleNamespace(EWrapper=Wrapper, EClient=Client))
    observer = PositionObserver(changes.append, lambda _generation, value: health.append(value))
    observer.start(ObservationSettings("DU1234567", 7497, 18, 17, 2))
    try:
        for _ in range(100):
            if health:
                break
            Event().wait(0.01)
        assert health == ["connected"]
        assert changes == []
        observer.stop()
        connected.clear()
        observer.start(ObservationSettings("DU1234567", 7497, 18, 17, 2))
        for _ in range(100):
            if changes:
                break
            Event().wait(0.01)
        assert len(changes) == 1
    finally:
        observer.stop()
