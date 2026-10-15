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

The application creates new app-owned SELL limit/stop OCA pairs **stop first**.
For each tranche, it sends the stop with `transmit=True`, verifies acceptance,
then sends its profit target with `transmit=True` in the same unique OCA group
and OCA type 2. IDs are allocated in ascending send order; receipts retain the
journal's target/stop ordering. TWS precautions remain active.

Before the first stop, before each target, and after each target, the writer takes
fresh all-open-order, position, and execution reads on the same client connection.
It checks full option identity, account/client/permanent IDs, prices, quantities,
OCA fields, accepted status, unchanged existing orders, unchanged position cost
and quantity, and the same execution IDs as the complete baseline. Every order
already sent must remain accepted and present. Reads complete within the original
bounded writer deadline; unique execution request IDs reject late prior responses.
A final callback check runs immediately before each send and after each read.
Neither permanent IDs alone nor absence from a partial snapshot proves a result.

A missing/changed stop prevents sending its target. A failed post-target pair
check stops all later tranches. Rejections, partial/full fills, disconnects,
timeouts, and unclassified TWS warnings leave the attempt uncertain; the app does not retry,
recreate a vanished stop, cancel a surviving order, or bypass a precaution.
The journal retains the failure reason, OCA identities and planned layers across
restarts. The blocking **Possible one-sided bracket** modal shows tranche cards
with target/stop prices and quantities, plus observed IDs/statuses in the failure
reason. Further changes to that contract stay blocked until a fresh snapshot
verifies healthy pairs or resolved terminal outcomes without surviving legs.
Refresh is available; **Verify bracket no longer exists** opens the existing
operator-confirmed, broker-checked absence workflow. Merely dismissing a dialog,
refreshing, or recording a fill cannot release an orphaned surviving order.
If the position closes during the attempt, its closed-position view retains the
warning. Complete, fresh, account/contract-scoped closed-history order evidence
must also show no surviving OCA group or recorded sibling ID before a terminal
fill releases the lock. This evidence expires and is invalidated on a failed
read; it never grants permission to submit or manage a closed position.

These reads are not atomic with submission. A stop can fill between the pre-target
check and target transmission. The post-target check detects changed evidence and
blocks further work, but cannot undo a sell or guarantee that OCA linkage will
prevent an orphan or overfill in every race. Paper validation with four or more
tranches remains required, including stop/target fills during creation, held or
rejected legs, manual cancellation, and restart after an uncertain result. A
`Submitted` or `PreSubmitted` status confirms broker acceptance, not execution.
TWS can still require its independent manual Transmit decision.

IBKR code 2109 (Outside RTH ignored for the order type/destination) is
informational: TWS continues processing the order. The probe, snapshot reader,
order listener and paper writers share the same explicit informational-code list.
This warning does not prove acceptance, promise execution outside regular hours,
or bypass a TWS precaution. Stop acceptance and all pre/post-target checks remain
mandatory; missing acknowledgements and genuine rejections still block the attempt.
The stop-first writer normalizes zero versus unset only for a price unused by
the order type (STP limit price, LMT auxiliary price). Both STP LMT prices remain
exact. It permits a returned `outsideRth=False` against the requested `True`
only after code 2109 for that exact submitted order ID; a warning for another
order cannot authorize that difference. Other mismatches identify the field
and expected/observed values in the blocking dialog, with account IDs redacted.

## Available-position trailing conversion

The paper-only **Set a trailing exit** action reviews every active,
app-owned OCA pair for the selected option and the current unassigned quantity.
This first version requires a USD-denominated option.
Coherent external SELL orders reserve their remaining quantity; complete
external target/stop OCA pairs reserve that quantity once. Conversion replaces
only journal-proven app-owned pairs and trails the held quantity minus these
reservations, including any genuinely unassigned contracts. The review shows
the trailing quantity and how many contracts remain reserved by unchanged TWS
orders. For example, ten MSTR contracts with five externally reserved and four
app brackets of 2 / 1 / 1 / 1 convert into one five-contract trail. There is no
manual quantity override: opening orders, invalid quantities, duplicate IDs,
malformed OCA groups, or excess exposure still block conversion.
The review requires a fresh bid, complete order and execution
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
LIMIT` order for the reviewed available quantity. The full held position is
tracked separately from the trailing quantity. Any change to retained order
identities, quantities, prices, or statuses stops conversion before the next
cancellation or submission, even when the total reserved quantity is unchanged.
A changed position, execution history, contract, or unusable quote also stops
the send. Each pair cancellation and the final
submission have separate durable journal records; an uncertain submission
locks management of that contract until the TWS state is reviewed. The app never
automatically retries the trailing submission.
Conversion holds one client lease through the entire cancellation, submission
and acknowledgement-refresh sequence, keeping the order listener paused rather
than reconnecting it between every bounded read and write. All fresh checks
remain in place, including a fresh bid for the final trailing plan. A stopped
conversion reports confirmed bracket cancellations, the underlying read or
validation failure, and whether trailing submission was attempted. If submission
may have occurred without an acknowledgement, its status remains unconfirmed
and the durable management lock remains. Partial conversions show a protection
warning with the last verified held quantity; they do not automatically resume.
Snapshot, cancellation and submission timing events share one run ID, with
account-redacted failure details, in the cancellation trace.
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
tests/test_source_safety.py` and the focused `tests/test_app_workbench.py tests/test_app_orders.py -k
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
legacy OCA target could have been staged with `transmit=False`; new brackets
send the stop first and both legs with `transmit=True`. A later amendment also
explicitly sets `transmit=True` on the selected
order. TWS order precautions remain enabled. The app requires both an
amendment callback and a subsequent `reqOpenOrders` result showing the exact
requested price before reporting success. If the fresh check still shows the
old price, the outcome is unknown and the user must inspect TWS before any
further change.

Bulk stop edits submit the same canonical decimal price
shown in the review, avoiding JavaScript binary floating-point tails such as
`1.6600000000000001` for a displayed `1.66`. The server still requires an exact
valid tick price and agreement with the return rounded for display.

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
## Working trailing order changes

A working, app-owned paper `TRAIL` or `TRAIL LIMIT` exposes **TRAIL AMT**
followed by **LMT offset**. Both accept dollars per contract or percentages;
the calculated premium price appears above each input. Dollar amounts are
divided by the verified contract multiplier. STP percentages use the fresh
bid; LMT percentages use STP and become a fixed dollar gap at review. The
stop rounds down and the limit gap rounds up using the verified market rule.
A dollar trail must already be on a valid increment. Editing only the offset
preserves the observed, potentially ratcheted stop; changing the trail rule
calculates a new initial stop from the fresh bid. The reviewed stop stays fixed
through confirmation, which requires a fresh bid above it.

An empty offset requests `TRAIL`; a positive offset requests `TRAIL LIMIT`.
The resulting limit must remain positive and below STP. Inline field feedback
and Action Review explain missing trails, invalid amounts, unavailable bids,
and excessive offsets. Review remains disabled until valid; the server repeats
the checks. Older absolute-price forms keep their explicitly named semantics;
the new distance fields cannot be interpreted as absolute prices.

Order quantity remains fixed. Review price changes takes fresh TWS reads and
checks the candidate against the current order before opening Cancel / Confirm
directly. The values and units remain visible and read-only until Cancel or
Confirm. Cancel restores the observed broker rule. The ten-second confirmation
expiry and final fresh-snapshot checks remain in effect; review sends no writes.
A separate Delete trail action cancels the exact order without replacement.
Both actions require review and final confirmation.

The service requires one journal-owned permanent ID and matching order ID,
account, contract, client ID, order type, GTC, current stop, trail amount,
limit gap, and full unfilled quantity. A trail covering only the unreserved
balance remains editable and cancellable beside coherent external exit orders.
The total observed closing allocation must still fit the held position. It
blocks partial fills, ambiguous or excessive external exposure, opening orders,
invalid tick increments, stale or disconnected
state, and manual changes observed after review. The writer requests the
same-client open order again and verifies these fields before using the
existing order ID; it leaves TWS order precautions active. For a gap change that
switches `TRAIL` and `TRAIL LIMIT`, the writer sends the new type with the same
order ID and requires an echo of the same permanent ID, new type, STP, gap, trail value, and the active trail unit. The unused
`auxPrice` or `trailingPercent` field is cleared when changing units; an
incomplete or mismatched rule echo is never accepted. A successful change
records the acknowledged rule in the durable journal for later management.
IBKR [cautions that order-type modifications may be rejected](https://interactivebrokers.github.io/tws-api/modifying_orders.html),
so a rejection or incomplete acknowledgement is treated as an unknown outcome
and locks further management until checked in TWS. This type switch requires
paper-TWS verification before any live milestone. A delete
requires a TWS cancellation status. Indeterminate outcomes are journaled and
lock further management across restarts until verified in TWS; neither action
is automatically retried. Successful deletion leaves the held contracts
unprotected. Paper behaviour must be checked in TWS before any live milestone.

After closure, the read-only row keeps the last recorded STP and LMT values;
they may precede the fill and are not a fill-price claim. If no trigger was
observed, the row shows an unavailable value rather than deriving one from a
later quote.

Trail action reviews share the bracket review component: `TRAIL-n`, remaining
quantity/TIF, linked SELL STP and SELL STP LMT rows, and old-to-new price arrows.
The LMT review row shows the actual limit price; adding or removing it
transitions from or to zero. An unchanged stop is shown once. Projected P&L
uses the shown LMT price, less unit basis, multiplied by contract multiplier
and remaining quantity; without LMT it uses the stop. For a $8.60 unit basis,
$8.10 LMT, multiplier 100, and three contracts, this is a $150 estimated loss. This is an estimate at the
shown prices, not a guaranteed fill or forecast of the trail's future movement.
The projection uses verified observed remaining quantities and the staged
candidate after review, without changing execution validation or order writes.
Verify with `.venv/bin/python -m pytest -q tests/test_app_trailing.py tests/test_app_browser.py`.

Trail-only positions, trail changes and bracket-to-trail conversion reviews
show one full-width `Projected P&L` card. It uses the shown trail limit, or the
stop trigger without a limit; stops can slip and limits may not fill. Brackets,
mixed bracket/trail positions, and trails with bracket drafts or pending
brackets retain both `Projected P&L` and `P&L at STP`. The latter is a
hypothetical result at the trigger, not a maximum loss or guaranteed fill price.
Cancellation-only reviews hide the entire projection until the review is
abandoned or completed, since cancelling an exit does not realise P&L.
These are presentation rules; projected amounts and execution checks are unchanged.
Tooltips state the price assumptions
and explain slippage and non-execution of stop-limit orders. Positive P&L and
return percentages are green, negative values are red, and zero or unavailable
values are neutral in both server reviews and local edit previews.

## Position toolbar for brackets and trails

The selected-position toolbar exposes **Set all stops…**, **Raise stops to
B/E**, **Cancel all exits**, and **Exit position now** for both working
brackets and a working trail. There is still at most one working trail for
an option, covering the available quantity after external reservations; multiple
app-owned trailing orders are not supported. All management remains paper-only and restricted to journal-owned
orders. An external order or ambiguous exit set blocks the position exit.

Set all stops applies a common premium trigger. For a trail, review derives a
fixed dollar trail distance from the fresh bid minus that trigger, rounding
the distance up to a valid increment. The explicit trigger is retained, and
the existing limit gap is preserved unless the user overrides it. Review
shows the changed trail rule, STP, LMT, and projected P&L. The dialog blocks
Apply if the bid is missing or stale, or the requested trail trigger is at
or above the bid; it explains the problem without changing either input.
Return percentages use entry basis: -20% of $8.60 is a $6.88 trigger.
Raise stops to B/E
only raises triggers below the entry basis and never lowers higher triggers;
a fresh read that would turn it into a lowering operation blocks review.
A trail's limit may still be below basis, so B/E is a trigger description,
not a guaranteed break-even fill.

Cancel all exits keeps the contracts. For the supported single trail it uses
the same exact-order cancellation and acknowledgement checks as row deletion.
Exit position now replaces the former toolbar action that sold only assigned
bracket quantities: it reviews all app-owned exit cancellations and one market
sell for the **entire held position**, including unassigned contracts. Row-level
market exits keep their existing per-layer scope.

The toolbar tooltip is **Sell all brackets now**. Its review retains the full
held quantity. The market sell still includes contracts without brackets. SELL MKT
shows an approximate price at the latest usable observed bid. Projected P&L
adds verified realised bracket P&L to `(bid - basis) * multiplier * held quantity`;
the return uses held plus verified sold contract cost, as elsewhere in the UI.
The estimate is display-only and never supplies a limit price to the market
order. Delayed bids are labelled; missing, stale or frozen bids are unavailable.
Unresolved realised or trailing-fill evidence leaves the total P&L unknown.
The market review hides the STP outcome, since those exits will be cancelled,
and states that market fills may differ and additional exit fees are excluded.

Position exit requires review, a fresh verification, and final confirmation
within ten seconds. It checks the reviewed position quantity, basis, full
contract, connection health, and execution IDs before each cancellation and
again before market submission. After cancellation, no working order for the
option may remain. The market writer reads all open orders, positions, and
executions on its own connection before writing; a changed quantity, cost,
fill set, or new order prevents the sell. It requires an exact SELL MKT/GTC
quantity acknowledgement with a positive permanent ID. Submission is durably
reserved before the writer; rejection or incomplete acknowledgement locks
management across restarts, and duplicate attempts are not automatically
retried. If cancellation succeeds but the position subsequently changes, the
sell stops and the UI reports how many cancellations completed; the contracts
can remain without exit protection.

The complete position-exit sequence holds one client lease, keeping the order
observer paused through all cancellations, fresh reads, market submission and
the acknowledgement refresh. This avoids repeated observer reconnects between
steps; the adapters still use bounded connections and retain every per-exit
snapshot and writer check. Cancellations are not blindly batched or retried.

If a read fails partway through, the UI preserves the underlying validation
reason and reports the number of confirmed cancellations and whether market
submission was attempted. A lost market acknowledgement remains unconfirmed,
with the durable management lock intact. A partial-exit banner labels the held
quantity as last verified; it is not evidence of current protection. Refresh
and check TWS before deciding how to manage remaining contracts. Position-exit
trace events share one run ID across snapshot and cancellation phases, recording
durations, progress and account-redacted failure reasons. Earlier logs that only
recorded a generic snapshot-unavailable error cannot establish its broker cause.

The adapter's `connection_epoch` advances on every bounded snapshot capture;
it is diagnostic metadata, not a persistent connection identity. A new positive
capture counter alone does not invalidate sell-all review or its bid estimate.
Every read must still be connected, complete and fresh, with unchanged account,
contract, quantity, basis, cost and fills. The exit-order set must also match
before cancellation or submission. Material baseline differences name the
changed fields, and a blocked market-exit review uses its own toast title.

Verify with `.venv/bin/python -m pytest -q`. These demo and automated checks do
not establish paper-TWS or live execution success. Paper-TWS verification is
still required before a separately authorized live milestone.


## Market exit layers and cancellation

App-owned market exits have their own MKT layer in the held-position workspace
and the session's closed-position history. All market submission paths save the
requested quantity before calling the transport and retain the acknowledged
order/permanent IDs. The row shows a disabled approximate bid field and remaining
quantity while working, with recorded partial fills shown separately. Its price
is an estimate, never a limit price. Unavailable bids show a dash. The MKT input spans both price columns; the
approximation symbol identifies an estimate. Confirmed complete SELL executions replace
that estimate with the actual weighted average fill price and retain realized
P&L in closed-position history. Disappearance alone remains CHECK TWS. Legacy
records without quantity need exact terminal Filled evidence and complete fills
before their quantity is recovered; partial executions alone cannot prove full
completion. Market fills remain in the durable journal across restart.

The trash control uses the same tooltip treatment as bracket and trail controls
and stages cancellation in the Action Review sidebar. The sidebar uses the shared
cancellation card, Cancel changes control, fresh Review step and ten-second final
Confirm window. There is no market-specific confirmation dialog. Review and
Confirm each verify the exact journal-owned same-client standalone SELL MKT/GTC
order; changed remaining quantity or order identity blocks cancellation. A direct
Confirm without a verified review, or after expiry, cannot send a cancellation. The isolated paper writer re-reads its own open orders and
checks account, contract ID, permanent ID, client ID, type, TIF, grouping, status
and remaining quantity before cancelling only that order ID. It accepts exact
terminal cancellation callbacks or code 202 for that ID after the request;
rejections, fills, timeout and incomplete acknowledgements remain uncertain.
A market order can fill between these checks and cancellation: the control does
not guarantee that a fill can be prevented. Unknown outcomes are durably locked,
with no automatic write retry. A later complete snapshot with exact linked
terminal cancellation evidence can resolve a lost cancellation acknowledgement.
External orders are never offered for cancellation by this layer.

Verify with `.venv/bin/python -m pytest -q`; focused domain/transport coverage is
in `tests/test_market_orders.py`, with UI lifecycle checks in
`tests/test_app_orders.py`. Paper-TWS callback timing and out-of-hours behavior
still require verification. These changes do not authorize live transmission.

### Journal transaction safety

Journal read/modify/write operations now share a reentrant lock per canonical
path within the process and a stable sidecar file lock between processes.
Simultaneous confirmations cannot both persist the same new intent; writes on
other contracts cannot overwrite each other's journal records. Process-level
contention blocks the operation before transport. The lock covers journal I/O,
never broker waits, and is released by the OS if the process exits.

This uses Unix `flock` on a local filesystem with working advisory locks.
Unsupported locking platforms fail closed. Every application writer must use
`ExecutionJournal`; editing or replacing its files externally is unsupported.
The sidecar lock file must remain in place while an instance is running.
Temporary JSON is flushed with `fsync` before replacement, followed by a parent
directory flush. This strengthens persistence ordering; it is not a claim that
all hardware/filesystems survive every power-loss scenario.

Intent read/write/flush/replacement failures prevent transmission. If transport
acknowledges a submission but its receipt cannot be persisted, the service
reports an unknown outcome and retains the blocking intent. Restart never
automatically resends that plan. Offline regression tests for these boundaries
and thread/process contention live in `tests/test_execution_journal.py`.


## Event-driven trailing cancellation observation

The desktop now retains an observation-only order listener on the configured
submitting client ID (normally 17), alongside the distinct position observer
(normally 18). It requests `reqOpenOrders` on that nonzero client and receives
own-client `openOrder` and `orderStatus` hints. It does not require a Master
Client setting or bind manual orders. Order callbacks only request immediate
fresh reconciliation; they never write broker or journal state themselves.

Bounded captures and every paper transport entry point borrow an exclusive
in-process lease for `(host, port, client ID)`. Borrowing disconnects and joins
the listener before another connection opens; nested borrows keep it suspended.
After the adapter disconnects, the listener reconnects and requests an own-client
baseline. Baseline changes across the handoff also trigger reconciliation, and a
15-second recovery hint handles omitted callbacks. Intentional handoffs reject
late callbacks by generation and do not signal a broker disconnect. A listener
that cannot release its socket/reader blocks the borrowing operation. External
processes are not covered by the in-process lease; TWS client-ID collision errors
still fail closed. This bridge keeps bounded snapshot/write verification intact;
a fully shared persistent capture/write socket is a possible later refinement.

Fresh complete order and execution history can mark an exact journal-owned
trailing order `CANCELLED_CONFIRMED` only when it is absent from working orders
and has a matching terminal `Cancelled` or `ApiCancelled` record. Partial fills
are retained; disappearance, `Inactive`, incomplete history, stale connection,
or contradictory evidence stays unresolved. Both `status` and `completedStatus`
are retained. Only exact known terminal values are recognized, with contradictions
blocking confirmation; descriptive completed-status text does not prove a result.
The cancellation label does not claim who initiated it.

Confirmed cancelled trailing rows use the same neutral grey badge and
"Remove this cancelled layer from view" tooltip as cancelled bracket layers.
Removing a trail sets a durable display-only flag after exact fresh cancellation
verification. It retains the journal entry, fills and P&L history across refresh
and restart, and sends no order or cancellation. Active or uncertain trails cannot be
hidden. A hidden row is shown again if current working-order evidence conflicts
with its cancellation or the selected snapshot is unavailable or unverified.

The trailing writer accepts cancellation code `202` only after sending a cancel
for that exact order ID. It also accepts exact `Cancelled`/`ApiCancelled` status
callbacks. A `202` for a different order, before the cancel, or during amendment
does not confirm success; other broker errors still block completion.
Before sending, the cancellation intent durably records its source trailing
fingerprint, order ID and permanent ID. If its acknowledgement is lost, fresh
complete reconciliation can complete that linked attempt when the exact source
has terminal cancellation evidence and fills have been reconciled. The snapshot
must be later than the intent. This works across restart and when a prior
refresh already marked the source cancelled. It does not clear unrelated
uncertain actions or infer cancellation from absence or a fill. Older intents
without an explicit source link retain the manual verification path; they are
not matched by guessing from the remaining order count. Failure details are
retained in the cancellation attempt for diagnosis.

The browser receives a protection-change notice through its existing two-second
revision stream. Draft and active inputs remain mounted until the operator uses
**Update view**; affected server-side confirmations are invalidated immediately.
A trailing conversion review survives a background hint only when a fresh
snapshot verifies the same position, fills, app-owned brackets, and external
reservations. Quote movement and a new capture connection epoch alone do not
invalidate it. Its original confirmation deadline is preserved. A queued hint
before Review or Confirm does not erase the conversion: those actions perform
their existing fresh safety checks, and writes still require explicit Confirm.
A changed safety input invalidates the review with a protection-change notice.
The updated view shows the cancelled trail and any partial-fill quantity, or
`CHECK TWS` when terminal evidence is missing. A confirmed trail cancellation is
recorded once and survives restart. This is observation and display behavior;
existing paper-only write gates and TWS precautions still apply.

Verification commands: `.venv/bin/python -m pytest -q`,
`.venv/bin/python -m mypy`, `.venv/bin/python -m ruff check src tests`.
Deterministic tests cover callback deduplication, wrong accounts, stale generations,
client-ID handoffs/collisions, serialized reader/writer leases, cancellation with
unchanged position, incomplete/conflicting history, partial fills and restart.
Actual callback delivery, reconnect gaps and terminal-history fields still need
the paper-TWS experiment described in
[the research note](research/TRAILING_CANCELLATION_CALLBACKS.md). No live trading
behavior has been added or validated.
