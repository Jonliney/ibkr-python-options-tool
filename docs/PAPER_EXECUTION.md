# Paper OCA execution

This is an explicit, paper-only milestone. It is enabled only with
`--enable-paper-execution`; the default desktop launch remains read-only.

## Execution contract

Choosing an active-layer action (delete bracket, delete all active brackets,
sell one layer, or sell all
active layers) first builds its Action review from the selected position's
already displayed snapshot. It does not contact TWS again or permit a write.
Changing active target/stop prices, including Move to B/E, likewise updates the
review before another broker read. **Execute paper order** then requests a fresh
snapshot and verifies the reviewed account, contract, app-owned order IDs,
quantities, prices, and OCA pairs. Only a matching result exposes **Confirm**.
Confirmation requests another fresh snapshot before any paper write. A changed
or incomplete snapshot blocks the write; cancellation and market-exit reviews
must be staged again if their verified orders change.

Draft submission, active price updates, bracket cancellation, and market exits
share the same visible sequence: inspect the action review, choose **Execute
paper order** to verify a fresh snapshot, then choose **Cancel** or the red
**Confirm** action. Confirm always performs another fresh broker-state check
before a paper write. The active-layer action icons create the initial review;
draft and price edits update that review as their fields change. A recovery
dialog that verifies a prior unknown cancellation is a separate read/verify
workflow and does not submit a new order.

**Delete all active layers** stages every currently reconciled, app-owned OCA
pair for the selected position. Confirmation cancels pairs sequentially, using
the existing two-leg cancellation and journal receipt for each pair. It reads
TWS again before each cancellation and stops if the remaining set changes, a
pair no longer matches the review, or an acknowledgement is incomplete. An
earlier pair may already be cancelled when a later pair fails; the status
reports the confirmed count and requires a fresh TWS review before another
action. No market sell is sent, so the position remains open.

For active stop amendments, the editor uses signed return from verified entry
cost. A positive value places the proposed stop above entry; 0% is B/E. The
global **Set all active stops** dialog applies one tick-rounded price to every
active app-owned layer for the selected contract. It starts at the current
stop when all layers share that price; otherwise it requires an explicit
entry. These are local proposals until the existing fresh-snapshot review and
confirmation gates succeed. A stop is a trigger and does not guarantee the
displayed gain at fill.
The dialog displays entry cost to cents while calculations retain the full
verified average cost. The proposed stop uses the selected contract and
exchange's verified IBKR market-rule bands; no symbol-specific tick size is
assumed for SPX, XSP, SPY, or other options.

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
- every existing selected-option closing order has a coherent, verified
  reservation; ungrouped SELL orders reserve their remaining quantity and
  complete target/stop OCA pairs reserve one pair quantity, regardless of who
  created them; the new plan fits the unreserved balance; and
- the new snapshot produces the same deterministic plan fingerprint the user
  armed on the first click.

External orders remain inspect-only. The app does not modify, cancel, or take
ownership of them. Their identity, status, and remaining quantities are part
of the plan fingerprint, so a change between review and confirmation blocks
submission. A concurrent manual change after the final snapshot is still
possible; the snapshot is not an atomic reservation at TWS. Paper trading and
TWS order precautions remain necessary checks before any later live milestone.

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
the UI does not claim that TWS accepted those orders. Another submission is
not offered while any bracket needs verification. A fully acknowledged but
unverified bracket reserves its journal quantity for draft planning even when
the broker snapshot omits the orders. The remaining quantity can be drafted,
but cannot be submitted until TWS is refreshed and the pending bracket is
reconciled. Unknown, partial, or missing-execution states continue to hide the
draft. Pending rows are restored from the journal after an app restart. Only
complete, broker-observed,
journal-proven pairs in `Submitted` or `PreSubmitted` status appear as manageable
active layers.

When adding a draft below pending rows, its default LMT target uses the lowest
configured preset whose rounded sell price is above all pending and current
draft LMT prices. If no such preset exists, Add Layer repeats the final
configured LMT preset. It can therefore share a target price with a pending
row. The stop preset also repeats its final value when the list is exhausted.
These defaults do not authorize submission; pending brackets still require
TWS verification before another draft can be submitted.

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

The price-amendment review compares modified SELL STP prices with the latest
option ask and modified SELL LMT prices with the latest bid. A crossing quote
warns that the leg may execute soon and close its OCA bracket. Missing,
delayed, or frozen quotes are identified as uncertain; a quote is never a fill
guarantee, and TWS trigger methods or later market movement can change the
outcome. Confirmation refreshes broker state again. If that refresh introduces
an immediate-sell concern not shown during review, no amendment is sent until
the operator reviews the new warning and confirms again.

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
After a submission, the workbench prompts the operator to check TWS for any
required Transmit confirmation and refresh. This prompt confirms that order
requests were sent to TWS; it does not claim that TWS accepted or transmitted
every leg. The journal remains the authority for whether another submission
can be attempted.
When a selected position already has an OCA order group in the TWS snapshot,
the workbench displays that exposure without creating a draft layer. The
operator must use **Add layer** to plan additional uncommitted quantity.
For an app-initiated bracket cancellation, the writer waits for both leg
cancellation acknowledgements and rechecks its open orders. The workbench then
refreshes the selected position and shows **Bracket cancelled** only when
neither leg remains working. A conflicting or incomplete refresh stays
unresolved and requires inspection in TWS before further action.

If the operator cancels an unacknowledged bracket in TWS before the app
reconciles it, a later fresh refresh can retire that unknown attempt only when
the broker reports a complete completed-order and execution snapshot showing
both exact fingerprinted legs of every planned layer cancelled with nonzero
permanent IDs, no matching fills, and no matching working order. If any of
that evidence is missing, the attempt remains blocked against duplicate send.
For a precaution-held or untransmitted leg that never appears in completed-order
history, or a previously acknowledged or reconciled app bracket whose legs
disappear from working orders without a matching fill, the workbench opens a
blocking **Verify cancellation** dialog. The
operator must first confirm in TWS that both LMT and STP are gone. The app then
takes a fresh selected-contract snapshot and requires complete current and
completed order reads, complete execution history, no matching working leg or
execution, no conflicting completed status, and enough held quantity for the
original plan. The dialog remains open if these checks fail. The journal records
the confirmation and snapshot time. Sending
the same draft again still requires a later clean snapshot; the confirmation
alone never authorizes an order write.
Executions on other tranches of the same contract do not block this recovery
when their permanent order IDs map to different OCA groups in the broker order
read or to another saved app submission. BUY executions cannot belong to the
SELL bracket being cancelled. An unmatched SELL execution remains ambiguous and
blocks recovery; the dialog reports its permanent order ID for checking in TWS.
The workspace trash control on a confirmed cancelled layer only marks that row
as hidden. It uses the exact saved cancellation record and refuses a working
leg or matching fill already visible in the current snapshot or journal. It
does not require a new complete execution-history read for this display-only
action. Repeated plans can share a fingerprint and OCA group, so the row action
also carries the saved attempt capture time and checks that attempt's order IDs.
The full journal entry,
order IDs, and duplicate-submission history remain durable; a later conflicting
broker outcome can make the layer visible again.

## Required paper validation

Market-closed/deterministic tests verify the plan and submission protocol, but
they do not prove broker behaviour. Before considering a live-account
milestone, test in paper TWS that each limit/stop pair is accepted, grouped as
expected, transmits atomically enough for the intended workflow, reports its
permanent IDs, and reacts correctly to fills/cancellations/reconnects. Also
test that the selected pair is cancelled without affecting other OCA groups,
the post-cancel open-order recheck is clean, and the standalone MKT produces
the expected order/execution callbacks.
