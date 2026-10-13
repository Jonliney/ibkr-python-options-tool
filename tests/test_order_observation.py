from threading import Event
from types import SimpleNamespace

import pytest

from ibkr_options_manager import order_observation
from ibkr_options_manager.order_observation import OrderObserver, borrow_client


def test_same_client_listener_handoff_callbacks_and_reconnect(monkeypatch):
    changes = []
    health = []
    clients = []
    active = set()
    connected = Event()

    class Wrapper:
        pass

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper
            self.closed = Event()
            self.active = False
            clients.append(self)

        def connect(self, host, port, client_id):
            assert client_id == 17
            assert client_id not in active
            active.add(client_id)
            self.active = True

        def run(self):
            self.wrapper.nextValidId(1)
            self.closed.wait(2)

        def isConnected(self):
            return self.active

        def disconnect(self):
            self.active = False
            active.discard(17)
            self.closed.set()

        def reqOpenOrders(self):
            self.wrapper.openOrder(
                701,
                SimpleNamespace(conId=123),
                SimpleNamespace(
                    account="DU1234567", permId=801, orderType="TRAIL", totalQuantity=3
                ),
                SimpleNamespace(status="Submitted"),
            )
            self.wrapper.orderStatus(701, "Submitted", 0, 3, 0, 801)
            self.wrapper.openOrderEnd()
            connected.set()

    monkeypatch.setattr(
        order_observation,
        "_load_ibapi",
        lambda: SimpleNamespace(EWrapper=Wrapper, EClient=Client),
    )
    listener = OrderObserver(
        "DU1234567", 7497, 17, 1, lambda: changes.append("hint"), health.append
    )
    listener.start()
    try:
        assert connected.wait(1)
        app = clients[0].wrapper
        app.orderStatus(701, "Cancelled", 0, 0, 0, 801)
        app.orderStatus(701, "Cancelled", 0, 0, 0, 801)
        assert changes == ["hint"]
        app.openOrder(
            702,
            SimpleNamespace(conId=123),
            SimpleNamespace(account="DU9999999", permId=802),
            SimpleNamespace(status="Submitted"),
        )
        app.orderStatus(702, "Cancelled", 0, 0, 0, 802)
        assert changes == ["hint"]
        connected.clear()
        with borrow_client("127.0.0.1", 7497, 17):
            assert not active
            app.orderStatus(701, "Filled", 3, 0, 0, 801)  # prior generation
            assert changes == ["hint"]
            with borrow_client("127.0.0.1", 7497, 17):
                assert len(clients) == 1
            assert not active
        assert connected.wait(1)
        assert len(clients) == 2
        assert health == ["connected", "connected"]
    finally:
        listener.stop()
    assert not active


def test_listener_client_collision_and_invalid_client_fail_closed():
    with pytest.raises(ValueError):
        OrderObserver("DU1234567", 7497, 0, 1, lambda: None, lambda _: None)
    slot = order_observation._slot("127.0.0.1", 7497, 117)
    owner = OrderObserver("DU1234567", 7497, 117, 1, lambda: None, lambda _: None)
    contender = OrderObserver("DU1234567", 7497, 117, 1, lambda: None, lambda _: None)
    slot.listener = owner
    try:
        with pytest.raises(RuntimeError, match="already has"):
            contender.start()
    finally:
        slot.listener = None


def test_borrow_fails_before_writer_when_listener_cannot_stop():
    slot = order_observation._slot("127.0.0.1", 7497, 217)

    class Stuck:
        def _pause(self):
            raise RuntimeError("did not release")

    slot.listener = Stuck()
    try:
        with (
            pytest.raises(RuntimeError, match="did not release"),
            borrow_client("127.0.0.1", 7497, 217),
        ):
            pytest.fail("must not connect or write")
    finally:
        slot.listener = None


def test_leased_capture_and_writer_wait_for_each_other():
    from threading import Thread

    from ibkr_options_manager.order_observation import leased_client

    entered = Event()
    release = Event()
    written = Event()

    @leased_client
    def capture(request):
        entered.set()
        assert release.wait(1)

    @leased_client
    def writer(*, host, port, client_id):
        written.set()

    request = SimpleNamespace(host="127.0.0.1", port=7497, client_id=317)
    reader = Thread(target=capture, args=(request,))
    sender = Thread(
        target=writer,
        kwargs={
            "host": request.host,
            "port": request.port,
            "client_id": request.client_id,
        },
    )
    reader.start()
    assert entered.wait(1)
    sender.start()
    assert not written.wait(0.05)
    release.set()
    reader.join(1)
    sender.join(1)
    assert written.is_set()
