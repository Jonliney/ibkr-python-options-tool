# Paper OCA execution

This is an explicit, paper-only milestone. It is enabled only with
`--enable-paper-execution`; the default desktop launch remains read-only.

`--demo-data --enable-paper-execution` runs a local, socket-free rehearsal on
weekends. Its synthetic transport acknowledges bracket submissions and exact
price amendments, and later demo reads show the amended order prices from the
separate demo journal. The server still runs the normal review, journal, and
contract-lock checks. A deterministic lost-acknowledgement test covers the
uncertain-outcome lock. Demo acknowledgements do not establish how paper TWS
or live TWS will respond to an order or precaution.

## Execution contract

New bracket, trailing, and market-exit orders use GTC time in force. Drafts
and conversion dialogs do not offer a DAY choice; the deterministic planners
reject non-GTC requests, and paper submission checks the planned TIF again.
The app assumes its managed orders were created under this GTC policy. If a
different TIF appears in a broker snapshot, order management stops for review.

The persistent position subscriber supplies change hints and connection
health. A verified empty portfolio stays idle after its initial subscription;
manual Refresh reuses a healthy subscriber. An option-position event or a
reconnection still triggers a fresh portfolio capture. Subscription hints
never authorize an order action: the normal fresh-snapshot checks below remain
required.

Choosing an active-layer action (delete bracket, delete all active brackets,
sell one layer, or sell all
active layers) first builds its Action review from the selected position's
already displayed snapshot. It does not contact TWS again or permit a write.
Changing active target/stop prices, including Move to B/E, likewise updates the
review before another broker read. The **Review cancellation** or **Review market sell** button then requests a fresh
snapshot and verifies the reviewed account, contract, app-owned order IDs,
quantities, prices, and OCA pairs. Only a matching result exposes **Confirm**.
Confirmation requests another fresh snapshot before any paper write. A changed
or incomplete snapshot blocks the write; cancellation and market-exit reviews
must be staged again if their verified orders change.
After an acknowledged price amendment, the app also refreshes automatically.
That broker read can be verified even when an unrelated draft validation leaves
the planning page blocked. The position subscriber reports position changes;
it does not prove a stop-price amendment. If the automatic read cannot verify
TWS state, another order change remains gated by a fresh snapshot.

Draft submission, active price updates, bracket cancellation, and market exits
share the same visible sequence: inspect the action review, choose the relevant
**Review** action to verify a fresh snapshot, then choose **Cancel** or the red
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

Bulk bracket cancellation writes a local timing trace to
`~/Library/Application Support/IBKR Options Manager/logs/bracket-cancellations.jsonl`
on macOS (or beside the platform's paper execution journal). Set
`IBKR_OPTIONS_MANAGER_CANCEL_TRACE` to choose another path. Each JSONL event
uses one random `run_id`, a one-based bracket number where applicable, UTC
time, and monotonic elapsed milliseconds from the server handling the operator's
bulk-cancel click. It records review and confirmation triggers, fresh snapshot durations,
pair cancellation durations, TWS connection readiness, both-leg acknowledgement,
the post-cancellation open-order check, and the final refresh. It omits account,
contract, and order identifiers and rotates at 2 MB. A trace write failure does
not change the cancellation outcome.

For snapshots taken during this flow, the same trace also records the capture
kind and role (`review`, `before_pair`, or `final_refresh`), connection handshake,
each named TWS callback wait, quote snapshot and market-rule requests, optional
history waits, local capture assembly, and total capture duration. A callback
wait's `complete` field distinguishes an acknowledgement from a timeout. These
measurements identify which part of a fresh snapshot is slow without changing
the snapshot's completeness requirements.

The current bulk flow rechecks the remaining reviewed pairs after each
acknowledged cancellation. This detects a fill, manual TWS change, or missing
acknowledgement before sending the next pair's cancellations. A future batched
flow would need durable per-pair outcomes and a safe recovery path for partial
success; elapsed time alone does not justify skipping these checks.

Bulk cancellation review, pre-pair checks, final verification, single-bracket
cancellation, status recovery, and post-write refreshes use orders-only
snapshots. They still collect account, position, contract identity, TWS
configuration, client and all-open-order views, completed orders, executions,
and market rule. The UI labels market data `NOT_REQUESTED`. A new bracket still
has a separate quote-bearing review; its immediate pre-send confirmation uses
an orders-only snapshot and must reproduce the reviewed plan fingerprint.

Quote-bearing reads (including normal Refresh, order review, and price-amendment
warnings) first open a short-lived TWS streaming market-data subscription. The
app cancels it after bid and ask arrive, or after a short grace period if only
another positive price is available. Missing bid/ask remains visible as missing;
price-amendment risk is reported as unknown in that case. If the stream yields
no positive price promptly, the reader falls back to the prior one-time quote
snapshot and its `tickSnapshotEnd` completion. Quote data is never taken from
the browser as broker evidence. These choices retain fresh order and position
checks while avoiding the approximately 11-second one-time snapshot wait when
TWS delivers a usable stream promptly.

For active stop amendments, the editor uses signed return from verified entry
cost. A positive value places the proposed stop above entry; 0% is B/E. The
global **Set all active stops** dialog applies one tick-rounded price to every
active app-owned layer for the selected contract. It starts at the current
stop when all layers share that price; otherwise it requires an explicit
entry. Active stop fields show price-derived returns to two decimal places;
the selected tick-rounded price is kept separately for the review and checked
against that displayed return. These are local
proposals until the existing fresh-snapshot review and
confirmation gates succeed. A stop is a trigger and does not guarantee the
displayed gain at fill.
The dialog displays entry cost to cents while calculations retain the full
verified average cost. The proposed stop uses the selected contract and
exchange's verified IBKR market-rule bands; no symbol-specific tick size is
assumed for SPX, XSP, SPY, or other options.

Draft layers have separate **Set all draft stops** and **Move all draft stops to
B/E** controls beside their STP / STP LMT choice. They change local draft fields
only; the existing draft review and TWS confirmation still govern submission.
The bulk draft dialog opens in percentage mode with the first configured STP
loss preset, shown as a negative return from entry (25% loss appears as −25%).
The bulk price is rounded to the selected contract's verified market rule and
must stay below every draft target. A draft stop may be at or above entry, but
its trigger must remain positive and below its target. Draft percentage fields
express loss from entry, so 0% means B/E and a negative value means a stop
above entry. These controls do not amend active orders.
The bulk draft stop editor shows the requested loss percentage to one decimal
place and keeps the selected tick-rounded price separately. On save, the server
requires that price to be on the verified market rule and either match the
requested percentage's tick-rounded result or fall within the displayed
percentage's rounding range when the user entered a price. Editing a stop
percentage directly clears the separate price. The demo equity-option feed
uses penny increments; live contracts always use their fetched IBKR market
rule, which may differ by contract and price band.

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

## Entire-position trailing conversion

The paper-only **Set a trailing exit** action reviews every active,
app-owned OCA pair for the selected option and the current unassigned quantity.
This first version requires a USD-denominated option.
It blocks when any other working order exists for that contract, including a
manual TWS order. The review requires a fresh bid, complete order and execution
history, an integral long position, and the verified contract's market rule.
If the bid moves between staging and the fresh review, the review shows a
recalculated initial stop and limit offset before confirmation. Account,
contract, position, execution history, and selected bracket identities must
still match. The TWS capture counter advances on every fresh read and is not
treated as a stable session identity. Each read must independently verify its
connection. Confirmation takes another fresh snapshot before submission.
The app checks the exact pairs and position again before each cancellation.
After the last pair is acknowledged cancelled, it refreshes the position,
orders, fills, contract, and bid before submitting one SELL `TRAIL` or `TRAIL
LIMIT` order for the full position. A changed quantity or contract, any working
order, or an unusable quote stops the send. Each pair cancellation and the final
submission have separate durable journal records; an uncertain submission
locks management of that contract until the TWS state is reviewed. The app never
automatically retries the trailing submission.
The final snapshot is not an atomic reservation at TWS; a manual change after
that read can still race the send, so the acknowledged order must be checked in
TWS.

The dialog's dollar trail and dollar limit offset are entered per contract;
both are divided by the verified multiplier before the pure plan and TWS order
use quoted option-price units. The review shows the effective per-contract
offset after market-rule rounding. Percentage entries retain their quoted-price
references: the trail uses the bid and the limit offset uses the initial stop.
The trail may be a tick-valid dollar amount or a percentage of option premium.
The initial stop estimate is rounded down on the verified market rule. A
trailing-limit percentage is converted at submission to one fixed dollar
`lmtPriceOffset`, rounded up to a valid increment, using the initial stop as
its reference. It does not stay proportional as TWS moves the stop. TWS order
precautions remain active. Cancellation creates a period without the original
bracket protection; a trailing market stop can slip, and a trailing limit may
remain unfilled. This flow has been built for paper TWS only. Paper results do
not establish equivalent live stop or complex-order behaviour.

The layer display records trailing fills only from complete execution history
whose account, contract ID, permanent order ID, and SELL side match the
app-owned trailing order. It marks the trail sold in the closed-position view
only when those fills account for its full quantity. A missing or conflicting
fill remains unresolved. The displayed realised P&L sums TWS-reported
`realizedPNL` from a matching USD `commissionAndFeesReport` (or legacy
`commissionReport`) for each execution. Fees are not tracked or reconstructed
separately. The app waits briefly for those callbacks after `execDetailsEnd`;
if any P&L report is missing, the result stays pending until a later refresh.
A reported zero is displayed as zero. A broker callback or a disappeared
position alone is not evidence of the final outcome.

The protective leg may be a SELL `STP LMT` for a new paper draft. The
position-level choice defaults to STP; the connection settings can set a
session default. One positive percentage or dollar amount below each verified
stop trigger determines its limit price. The pure planner rounds that limit
**down** to the selected contract's market-rule increment. If rounding would
produce zero or less, it uses the lowest positive price on the verified market
rule instead. It still blocks a limit at or above the stop trigger or an
invalid market rule. A very low limit can expose the position to much larger
losses than the stop-trigger projection; TWS may also reject or hold the order
under its independent precautions. Saving a session default with no draft layers
does not validate it against the planner's implicit preview layer; the chosen
stop-limit offset is validated when an explicit layer is planned. Removing the
last draft layer clears that position's stop choice, so the next layer inherits
the session default; changing settings also clears stale choices for positions
without drafts. Existing drafts retain their chosen stop type, offset, and unit.
The reviewed plan, fingerprint, journal, and paper writer all carry both prices.
The writer
sends a single `STP LMT` leg
with `auxPrice` as the stop trigger and `lmtPrice` as the limit, alongside the
SELL LMT target in the same two-order OCA group. The writer rechecks price
increments before connecting to TWS. For the documented index-option family,
both legs retain the verified Outside RTH setting. New STP LMT brackets save
their dollar or percent offset on every journal layer. Active stop edits
recalculate and review both the stop trigger and sell limit using that saved
rule and the verified market rule. Moving all stops to break even keeps each
layer's rule. Set all active stops can optionally replace the rule for the
selected layers; the replacement is saved only after both prices are verified.
Older brackets without a saved rule remain view-only for price amendments.
The paper writer modifies the same app-owned order ID and checks both prices
in TWS after the write. A multi-layer edit is not atomic; an uncertain or
partial acknowledgement locks further management until reconciliation.
As with every stop-limit order, reaching the trigger does not guarantee a fill.

Verification commands for this stack are `.venv/bin/python -m pytest -q
tests/test_planner.py tests/test_execution.py tests/test_app_view_model.py
tests/test_source_safety.py` and the focused `tests/test_app_demo.py -k
'stop_limit'` server tests. Paper TWS testing during an eligible extended
session remains necessary; simulation is not evidence of identical live
behavior.

After a submission receives complete API acknowledgements, the workbench shows
an **Orders sent to TWS** toast and journal-backed **Pending TWS verification**
rows until a fresh snapshot verifies the orders as working.
Every unresolved row offers **Verify**. **Refresh layers** requests a fresh
broker read and updates the row only when the exact app-owned OCA pair or a
matching completed fill is observed. **Clear unverified bracket** stays disabled
until the operator confirms that neither leg is working and neither filled.
Clearing also requires a later, complete order and execution read with no
matching working leg or possible fill. A fill must be recovered as an execution;
if it cannot be recovered, the row stays unverified for investigation in TWS.
The operator's statement alone is never broker evidence.
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

If a verified position disappears from the portfolio during the current
session, Refresh makes a separate read-only history capture for that exact
contract. It requires a fresh, complete account, completed-order, and execution
read plus matching contract identity before adding evidence to the journal. The
history capture is deliberately marked ineligible for order actions. A missing
working order alone never proves a fill; without an exact execution and its
realised P&L report, the layer remains for TWS review and its P&L is unknown.
Cancelled brackets are omitted from the closed-position page. The realised
header sums only closed layers with verified P&L; unrelated cancelled or
unresolved rows do not erase that known amount.

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
only after two fresh snapshots prove the selected LMT and its SELL STP or SELL STP LMT peer are
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

The price-amendment review compares modified SELL STP and SELL LMT prices with
the latest option bid. A crossing quote
warns that the leg may execute soon and close its OCA bracket. Missing,
delayed, or frozen quotes are identified as uncertain; a quote is never a fill
guarantee, and TWS trigger methods or later market movement can change the
outcome. Confirmation refreshes broker state again. If that refresh introduces
an immediate-sell concern not shown during review, no amendment is sent until
the operator reviews the new warning and confirms again.

If an earlier price amendment is journaled with an unknown outcome, the app
locks order and draft changes for that account and contract, including after
a restart. The operator may refresh or select another contract. To release the
lock, the operator must inspect the orders and fills in TWS, confirm no change
is awaiting Transmit, and obtain a later, complete broker read with stable
working orders and complete order and execution evidence. That manual
verification is recorded in the journal; it does not claim the amendment
succeeded. Retrying the exact price change still requires a fresh snapshot
showing the same app-owned orders at their old prices and a separate explicit
confirmation. A changed or incomplete snapshot blocks the retry. Unknown
cancellation and market-exit outcomes use the same contract lock and TWS
verification gate; their attempts remain non-retryable.

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
history, the workbench opens a blocking **Verify cancellation** dialog. A
previously reconciled bracket whose legs disappear without a matching fill
shows a **Verify** action on its row instead. This avoids opening an old
cancellation dialog over a newly working bracket; an unresolved old layer
still reserves its quantity until verified. An explicit **Verify** click opens
the dialog even if a malformed order group made the last planning snapshot
unavailable; opening the dialog does not clear the journal or authorize an
order. The
operator must first confirm in TWS that neither leg is working or filled. The app then
takes a fresh selected-contract snapshot and requires complete current and
completed order reads, complete execution history, no matching working leg or
execution, no conflicting completed status, and enough held quantity for the
selected layer. The row dialog displays only that layer's OCA pair, and confirmation
marks only that layer cancelled in the journal. Sibling layers keep their prior
working or filled state. An execution whose order identity cannot be attributed
to a different pair blocks clearance. The dialog remains open if these checks fail.
Sending
the same draft again still requires a later clean snapshot; the confirmation
alone never authorizes an order write.
Final paper confirmation for new brackets, price updates, bracket cancellation,
and market exits expires after 10 seconds. An expired confirmation never sends
an order change. The Confirm button shows the remaining seconds and returns to
the Execute step at zero; its browser countdown is informational, while the
server deadline controls whether submission is allowed. Price updates always
require this confirmation, including when the latest quote raises no new
immediate-sell warning.
Recreating a verified cancelled plan keeps its fingerprint for duplicate
suppression but persists a new OCA group prefix for the new attempt. Journal
reconciliation uses that prefix rather than attributing an earlier order to
the new bracket. Legacy attempts that already reused an OCA group cannot be
reconciled automatically: the workbench shows a conflict and directs the
operator to resolve every order in that group in TWS before verifying the
uncertain attempt. It must not invite Transmit for a mixed old/new group.
TWS may hold an API order for a manual Transmit decision, and untransmitted
orders can be absent from API open-order reads; a fresh read alone is not
proof that these orders were cancelled. The ordinary uncertain-order state
therefore tells the operator to check both legs in TWS, transmit there only
when the reviewed pair is correct, then Refresh in the app. TWS order
precautions remain enabled.
See [IBKR's untransmitted-order behavior](https://interactivebrokers.github.io/tws-api/order_submission.html)
and [OCA group semantics](https://interactivebrokers.github.io/tws-api/oca.html).
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
For a partially reconciled submission, saved order ID lists contain only the
surviving pairs; the trash check never treats their positions in that list as
the missing layer's IDs. It uses exact layer permanent IDs and checks the
layer's OCA group when one of those IDs is still unknown.
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
Trailing layer prices are read-only observations. For a working trail, the app records the latest positive `trailStopPrice` reported by TWS for the exact app-owned order; `auxPrice` is the trailing amount and must not be displayed as the stop. For a trail limit, the displayed limit is calculated from that stop and the journaled limit offset. After closure, the row keeps the last recorded values; they may precede the fill and are not a fill-price claim. If no trigger was observed, the row shows an unavailable value rather than deriving one from a later quote.
