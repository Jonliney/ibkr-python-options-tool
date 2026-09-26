# Paper OCA execution

This is an explicit, paper-only milestone. It is enabled only with
`--enable-paper-execution`; the default desktop launch remains read-only.

## Execution contract

Immediately before either confirmation is armed or confirmed, the application
requests a new TWS snapshot. Submission is allowed only when that snapshot
proves all of the following:

- the configured account is a managed `DU` account;
- TWS is connected on loopback and API read-only mode has explicitly reported
  `false`;
- positions, open orders, contract details, quote, and market-rule barriers
  completed inside the freshness window;
- the exact option identity agrees on account, `conId`, security type, expiry,
  strike, right, multiplier, currency, trading class, exchange, and local
  symbol;
- every existing selected-option order is either a complete, journal-proven
  app-owned OCA pair (whose remaining quantity is reserved) or absent; and
- the new snapshot produces the same deterministic plan fingerprint the user
  armed on the first click.

The application transmits new app-owned SELL limit/stop OCA pairs. For each
pair, it submits the limit order with `transmit=False`, followed by the stop
with `transmit=True`, using the pair's unique OCA group and OCA type 2.
TWS may still apply its own order precaution and require the user to click
**Transmit** there. The application must not bypass that independent TWS
safety control.

After a submission receives complete API acknowledgements, the workbench shows
an **Orders sent to TWS** toast and journal-backed **Pending TWS verification**
rows until a fresh snapshot verifies the orders as working.
These rows show the recorded plan, not permission to modify the orders. A
timeout or incomplete acknowledgement is labelled **Outcome not confirmed**;
the UI does not claim that TWS accepted those orders. While either state is
unresolved, the same draft is hidden and another submission is not offered.
The broker snapshot's available quantity is not treated as available for a new
draft while pending orders may be absent from that snapshot. Pending rows are
restored from the journal after an app restart. Only complete, broker-observed,
journal-proven pairs in `Submitted` or `PreSubmitted` status appear as manageable
active layers.

## Closed bracket history

On refresh, the selected-contract snapshot also requests completed API orders
and executions from TWS. The app joins a completed order to a planned layer by
the exact account, contract ID, fingerprinted OCA group, order type, and
permanent order ID. Executions are then joined by the permanent ID. This also
recovers order IDs for older journal entries whose partial reconciliation kept
only the surviving working pair. Closed and active layers stay in journal order
in the same layer list. The OCA group ID is shown in a tooltip on the layer
label. New journal entries keep the target percentage and the stop percentage
derived from the submission-time reference price for this display. For older
entries without those values, the UI shows approximate percentages only when
both recorded prices uniquely identify a pair of configured presets under the
contract's market-rule increments. Otherwise it shows an unknown percentage
beside each recorded planned price. These display values do not authorize an
order change.

A complete exit fill is labelled profit, loss, or flat only when TWS supplies a
realized P&L report for every matching execution in one currency. A fill without
that report is labelled **P&L unavailable**. Partial fills and missing execution
evidence stay flagged for TWS review; a vanished open order is never called a
profit or a loss from its planned target or stop price. Recorded fills are kept
in the local journal across restarts. IBKR execution queries normally cover
the current trading day; a wider window depends on the TWS Trade Log setting.
Older fills outside that window cannot be reconstructed if they were never
observed by this app. History evidence is for display only and does not grant
permission to amend or cancel an order.

The initial active-layer management action is deliberately narrow: **Sell now
(MKT)** can exit one app-created, fully reconciled OCA layer. It is available
only after two fresh snapshots prove the selected LMT and its SELL STP peer are
the complete two-leg OCA pair, have the same remaining quantity, were created
by the configured API client, and are proven app-owned by the journal. The
writer cancels exactly those two order IDs, waits for both cancellation
acknowledgements, re-reads this API client's open orders to prove neither leg
remains active, and only then submits one new standalone SELL MKT for the
verified quantity. It never cancels a different layer, uses global-cancel, or
touches manual orders. Any rejection, timeout, or incomplete acknowledgement
fails closed with no retry.

Price-only amendments use the same app-owned, same-client order IDs. The
original OCA target is staged with `transmit=False` and its stop sends the
pair, so a later amendment explicitly sets `transmit=True` on the selected
order. TWS order precautions remain enabled. The app requires both an
amendment callback and a subsequent `reqOpenOrders` result showing the exact
requested price before reporting success. If the fresh check still shows the
old price, the outcome is unknown and the user must inspect TWS before any
further change.

If an earlier price amendment is journaled with an unknown outcome, the app
requires a later fresh snapshot showing the same app-owned orders at their old
prices. The operator must also inspect TWS and explicitly confirm that no
amendment is waiting for Transmit. Only then can a new, separately journaled
price-only attempt be sent. The uncertain attempt remains in the audit trail;
other management operations remain non-retryable. If the broker snapshot has
changed or cannot be established as later than the unknown attempt, the app
blocks the recovery.

Each price-update confirmation writes a local JSON Lines trace to
`~/Library/Application Support/IBKR Options Manager/logs/price-amendments.jsonl`
on macOS (under the journal's state directory on other platforms). Set
`IBKR_OPTIONS_MANAGER_PRICE_TRACE` to an absolute path to override it. The
file records the staged request, submitted order fields, selected TWS
`openOrder`/`orderStatus` callbacks, TWS errors and warnings, the post-write
open-order check, and the UI result. It rotates at approximately 2 MB to a
`.1` backup and is limited to the current user. Treat the trace as private
brokerage data. A trace file that cannot be opened blocks the amendment before
the broker write; a trace failure after sending leaves the outcome unknown.

## Acknowledgement and recovery

The app waits for an `openOrder` acknowledgement carrying a nonzero permanent
order ID for every submitted order. The staged MKT experiment is stricter: it
needs cancellation evidence for both selected original order IDs, an
API-client open-order recheck showing neither is active, and an `openOrder`
acknowledgement with a nonzero permanent ID for the new standalone MKT. The
complete outcome is recorded in the local journal at:

- macOS: `~/Library/Application Support/IBKR Options Manager/`
- other platforms: `$XDG_STATE_HOME/IBKR Options Manager/` (or
  `~/.local/state/IBKR Options Manager/`)

The fingerprint is journaled as `PREPARED` before the TWS writer opens. Any
writer failure, timeout, or incomplete acknowledgement becomes
`SUBMISSION_UNKNOWN` and blocks that fingerprint from automatic retry. The
next refresh requests both the configured API client's own open orders
(`reqOpenOrders`) and the all-orders inspection view (`reqAllOpenOrders`). If
the client-bound view proves the exact expected complete OCA pairs with the
application's fingerprinted OCA-group prefix and permanent IDs, the journal
recovers them as `RECONCILED` and exposes them in **Active layers**. The
all-orders view alone is never sufficient for management because IBKR does not
bind those rows and can report API order ID `0`.

If that exact recovery check does not pass, the outcome remains unknown: the
application will not retry, adopt a partial pair, or modify anything. Inspect
TWS and create a deliberately new draft only after resolving the state.

## Required paper validation

Market-closed/deterministic tests verify the plan and submission protocol, but
they do not prove broker behaviour. Before considering a live-account
milestone, test in paper TWS that each limit/stop pair is accepted, grouped as
expected, transmits atomically enough for the intended workflow, reports its
permanent IDs, and reacts correctly to fills/cancellations/reconnects. Also
test that the selected pair is cancelled without affecting other OCA groups,
the post-cancel open-order recheck is clean, and the standalone MKT produces
the expected order/execution callbacks.
