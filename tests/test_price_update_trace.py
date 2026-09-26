import json
import stat

from ibkr_options_manager import price_update_trace


def test_price_update_trace_is_private_and_bounded(monkeypatch, tmp_path) -> None:
    path = tmp_path / "price-amendments.jsonl"
    monkeypatch.setenv("IBKR_OPTIONS_MANAGER_PRICE_TRACE", str(path))
    monkeypatch.setattr(price_update_trace, "_MAX_TRACE_BYTES", 50)

    assert price_update_trace.record_price_update_event("first", order_id=101)
    assert price_update_trace.record_price_update_event("second", order_id=101)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["event"] == "second"
    assert json.loads(path.with_name(path.name + ".1").read_text())["event"] == "first"
