# Detecting cancellation of an app-created trail in TWS

Researched 2026-10-13. These are documented API capabilities and design recommendations, not a record of a successful paper-TWS experiment. Current IBKR documentation now redirects to `interactivebrokers.com/docs/tws-api`; the legacy GitHub guide is labelled deprecated.

## Intended client configuration

The application uses a short-lived capture/writer client (default 17) and a persistent observation client (default 18); `ObservationSettings` requires different positive, nonzero IDs. The observer currently subscribes only to positions. Captures request the submitting client's open orders, then all open orders; selected-contract captures additionally request completed orders and executions. They disconnect after capture. See `src/ibkr_options_manager/observation.py`, `app/main.py`, `app/view_model.py`, and `broker/ibkr.py`.

An unfilled cancellation does not change the position. Therefore position callbacks alone cannot reliably trigger detection (design inference).

## Documented callback visibility

| Callback/request | What it establishes | Implication for this configuration |
| --- | --- | --- |
| `orderStatus` | Status, filled/remaining quantities, permanent ID and submitting client ID; messages can be duplicated. | The connected submitting client receives automatic status changes. A separate ordinary nonzero observer has no documented automatic cross-client stream. |
| `openOrder` / `openOrderEnd` | Contract/order/state details for open orders and an end marker. | Useful for snapshots and opportunistic hints; disappearance is not a cancellation reason. |
| `execDetails` / `execDetailsEnd` | Executions, and completion of a requested execution response. | Needed to distinguish a fill or partial-fill race from a cancellation; an unfilled cancellation produces no execution. |
| `reqAllOpenOrders` | One-time current open-order snapshot across associated accounts. | Does **not** subscribe the observer to subsequent changes. |
| `reqCompletedOrders` / `completedOrder` / `completedOrdersEnd` | Orders no longer modifiable for the given day, including executed, rejected and user-cancelled orders. | Gives positive terminal evidence after a fresh request, within its history window. |

Sources: [current order status reference](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/introduction), [open orders](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/open-orders), [execution callbacks](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details/receive-execution-details), [all submitted orders](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/all-submitted-orders), [completed-order scope](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/retrieving-completed-orders/introduction), [completed callback/end marker reference](https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html).

IBKR documents Master Client ID as the mechanism for automatic order/trade data from other API connections. Client ID 0 has special manual-TWS/FIX visibility. The legacy guide explicitly describes own-submitter `orderStatus` delivery and warns that callbacks are not guaranteed for every transition, recommending execution monitoring too. This supports a snapshot fallback even with a callback-capable connection. [Current client-ID documentation](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/client-id-0-and-the-master-client-id); [legacy automatic-status and completeness notes](https://interactivebrokers.github.io/tws-api/order_submission.html).

`PendingCancel` and `PreCancelled` are not final: fills can still arrive. `Cancelled` confirms cancellation of the remaining balance, but may also result from rejection. A status alone does not prove who initiated cancellation. Prefer “Trailing exit cancelled” / “Cancellation detected in TWS” to an unsupported claim that the user manually cancelled it. [IBKR status meanings](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/understanding-order-status-message).

## Completed orders and ownership

`reqCompletedOrders(False)` includes TWS-submitted orders; `True` requests only API orders. An API-created trail cancelled using the TWS UI is still originally API-submitted: the docs do **not** establish that `True` excludes that scenario. Keep the existing `True` for this fix unless paper evidence identifies a need to broaden it. Continue matching application ownership using journal identity, account, contract and permanent ID; external history does not grant management permission. [Completed request parameter](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/retrieving-completed-orders/requesting-completed-orders).

The current completed-order callback page has inconsistent names/signatures in its sample. The installed `ibapi.wrapper.EWrapper` confirms `completedOrder(contract, order, orderState)` and `completedOrdersEnd()`; follow the installed interface rather than copying that sample. [Current completed callback page](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/retrieving-completed-orders/receiving-completed-orders).

No Master setting or client-0 binding is necessary for bounded snapshot reconciliation. `reqOpenOrders()` at client 0 binds manual orders, making them modifiable/cancellable; `reqAutoOpenOrders(True)` also binds future manual orders and is client-0-only. Retain nonzero IDs and avoid these mechanisms merely to observe a cancellation. [Own-client requests and binding](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/api-clients-orders); [automatic manual-order binding](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/manually-submitted-tws-orders).

## Concrete local gaps

- [Observer](../../src/ibkr_options_manager/observation.py#L115) has only position callbacks and subscribes with `reqPositions` at line 186. The [surface observation loop](../../src/ibkr_options_manager/app/web/surface.py#L488) refreshes only when `_pending_observation` is set (line 514), leaving unchanged-position cancellations without an automatic trigger.
- [Browser inventory updates](../../src/ibkr_options_manager/app/web/surface.py#L3406) replace the sidebar inventory; unchanged selection does not repaint active protection rows. A new snapshot alone therefore needs an order/protection-change notice or row refresh as well.
- [Completed-order journal reconciliation](../../src/ibkr_options_manager/execution.py#L1537) requires a 64-character bracket fingerprint, excluding `trailing-conversion:` entries. Execution and trailing-price recording do not supply the missing cancellation transition.
- The [manual-cancel demo fixture](../../src/ibkr_options_manager/app/demo.py#L369) removes the working trail without positive terminal history. Its [current test](../../tests/test_app_demo.py#L482) intentionally expects `CHECK TWS`. That is appropriate for missing evidence, but is not a test of a positively confirmed cancellation.
- A deterministic harness using that fixture and injecting an exact owned `ObservedCompletedOrder` (`SELL`, `TRAIL LIMIT`, `Cancelled`, matching account/conId/permId/orderId/client 17), complete terminal history, no selected-contract working trail, and unchanged quantity 3 still leaves the trailing journal entry `SUBMITTED` after all three recording methods. Unrelated NVDA working orders remain; those do not contradict absence of the selected trail. This is local simulated evidence, not an observed broker event.
- [The capture adapter](../../src/ibkr_options_manager/broker/ibkr.py#L406) currently retains `orderState.status` only. Installed `ibapi.order_state.OrderState` also has `completedStatus`. Paper logs must establish which field conveys terminal status; do not equate arbitrary descriptive `completedStatus` text with an API status enum or silently override contradictory fields.

## Feasible improvements

1. First deliver a pure trailing-outcome classifier and journal transition based on complete fresh open-order and terminal/execution evidence for the exact app-owned identity. A missing trail without terminal evidence stays unresolved; timeout, disconnect or conflicting evidence must not be treated as cancellation. Preserve partial fills alongside cancelled remaining quantity; `Inactive` is not cancellation proof.
2. Add bounded periodic reconciliation while app-created trails need monitoring, followed by a browser notice or row refresh when protection changes without a position-quantity change. A cancellation must be detected even if no callback arrives.
3. Implement order/status/execution callbacks on the observer as deduplicated refresh hints when delivered. Never promise guaranteed immediate notifications from the default distinct non-Master observer. Keep generation guards, coalescing and reconnect reconciliation.
4. Retain completed-history uncertainty: its scope is the given day, so absence after restart or rollover is not positive cancellation evidence. Keep `apiOnly=True` until evidence justifies a broader request.
5. Preserve explicit paper verification as outstanding until an app-created trail is actually cancelled in TWS under the intended client configuration.

## Paper verification still needed

Record TWS/API versions, configured Master Client ID, account and app client IDs. Cancel a working app-created trail in TWS while observer 18 stays connected and writer 17 is disconnected; record which callbacks, if any, reach 18 and verify snapshot-driven UI update. Repeat during an active client-17 capture, after reconnect/restart, with partial fills, and with unavailable terminal history. Check `True` versus `False` completed-order responses for the same permanent ID. Use paper trading only; do not change binding/Master settings or submit live orders to satisfy this investigation.


## Real-time path clarified (2026-10-13)

The repository's automatic new-trade display is a real position subscription,
not a five-second broker polling loop: `position()` detects a quantity/basis
change, `_position_hint()` wakes the reconciliation worker, and a complete
refresh updates inventory. The browser receives revision/health heartbeats every
two seconds. The five-second timer in `_start_observer_locked` is a reconnect
retry deadline, not an order refresh interval.

This demonstrates that event-triggered verification can feel immediate while
retaining fresh snapshots. An unfilled cancellation needs the analogous
`orderStatus` signal, because it does not change position quantity.

Two documented ways to receive the app-created order's automatic updates are a
persistent connection using the submitting client ID, or an observer connected
as the configured Master Client ID. The first requires coordinating the current
short-lived capture/write connections: opening two sockets with the same client
ID is not a valid shortcut. The second requires an explicit TWS configuration
choice and paper verification; do not silently enable it. An order callback
should immediately invalidate affected reviews and trigger fresh reconciliation,
with the UI showing verification pending before a proven final outcome. Periodic
reads serve recovery for missed callbacks and reconnects, rather than determining
normal event latency. Source: the client-ID and order-status references above.
