from decimal import Decimal
from threading import Event
from types import SimpleNamespace

from ibkr_options_manager.broker.ibkr import _capture, _OrderDraft


def _order(*, order_id: int) -> _OrderDraft:
    return _OrderDraft(
        perm_id=201,
        client_id=17,
        order_id=order_id,
        account="DU1234567",
        con_id=917_864_414,
        action="SELL",
        order_type="LMT",
        oca_group="aabbccddeeff/tranche-1",
        parent_id=0,
        limit_price=Decimal("1.20"),
        stop_price=None,
        tif="GTC",
    )


def test_capture_prefers_client_bound_order_over_nonbinding_all_order_view() -> None:
    app = SimpleNamespace(
        all_order_drafts={0: _order(order_id=0)},
        all_order_statuses={0: ("Submitted", Decimal("2"))},
        client_order_drafts={101: _order(order_id=101)},
        client_order_statuses={101: ("Submitted", Decimal("2"))},
        events={"quote": Event()},
        quote_values={},
        market_data_type="UNKNOWN",
        completion_times={},
        positions=[],
        contract_details=[],
        server_version=None,
        server_time=None,
        read_only_api=False,
        localhost_only=True,
        managed_accounts=(),
        market_rule=None,
        errors=[],
    )

    capture = _capture(app, epoch=1, connected=True)

    assert len(capture.orders) == 1
    assert capture.orders[0].perm_id == 201
    assert capture.orders[0].order_id == 101
