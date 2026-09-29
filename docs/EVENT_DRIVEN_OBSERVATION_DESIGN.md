# Event-driven TWS observation and alerts

Status: first position-observation slice implemented pending paper-TWS verification
(2026-09-29). Scope: the
paper-TWS desktop workbench. This document authorizes no new order submission,
modification, cancellation, binding, or live trading behavior.

The first implementation slice is automatic position discovery and display.
Newly verified positions appear in the sidebar with a **NEW** badge, without
changing the selected contract or replacing unsaved LMT/STP input. Automatic
draft-layer creation from those settings is a later milestone. Order and fill
alerts below remain later slices.

## Goal and user-visible contract

Keep the portfolio and selected option current without requiring the operator
to press Refresh. Announce a newly verified option position, show material
changes to protective orders, and distinguish an app-submitted bracket whose
two legs are verified in TWS from one merely sent to TWS. Display connection
health and the time of the last complete broker observation. Preserve manual
Refresh as a recovery control.

An alert describes **observed broker state**, never a prediction. A callback
alone must not claim that an option was acquired, a bracket transmitted, a
position sold, or an order cancelled. The existing fresh-snapshot checks before
paper writes remain mandatory.

## What TWS can provide

| Signal | Use | Important limit |
| --- | --- | --- |
| `reqPositions` → `position`, `positionEnd` | Initial inventory, then changes to account positions | This is a subscription; the initial sequence is a baseline, not a set of new-position alerts. [IBKR positions](https://interactivebrokers.github.io/tws-api/positions.html) |
| `execDetails`, `commissionReport` | Fill hints, including partial fills and later P&L evidence | A separate observer client ID is not guaranteed to receive every execution or commission. Execution corrections require revision handling. A fill hint is not a complete account or order snapshot. [IBKR executions](https://interactivebrokers.github.io/tws-api/executions_commissions.html) |
| `openOrder`, `orderStatus`, `error` | App-owned order activity and hints for a changed bracket | Status callbacks may be duplicated or omitted. `execDetails` is also needed. [IBKR order status](https://interactivebrokers.github.io/tws-api/order_submission.html) |
| `reqAllOpenOrders` → `openOrder`, `openOrderEnd` | Read all currently visible working orders without binding manual orders | It returns a point-in-time list, **not a subscription**. Re-request it to detect manual TWS bracket creation, price edits, and removal. [IBKR open orders](https://interactivebrokers.github.io/tws-api/classIBApi_1_1EClient.html) |

Do not use client ID 0, `reqAutoOpenOrders`, or a binding variant of
`reqOpenOrders` to observe manually entered orders. Binding can grant control
and may affect a working order. Retain the existing nonzero client ID and the
read-only `reqAllOpenOrders` inspection path. [IBKR order retrieval](https://interactivebrokers.github.io/tws-api/open_orders.html),
[IBKR order modification](https://interactivebrokers.github.io/tws-api/modifying_orders.html).

There is no general “new option contract” webhook. For this product, a new
`(account, conId)` in the position subscription is the useful signal. Resolve
and verify its full option identity through the existing portfolio/selected
snapshot rules before showing it as available.

## Observation architecture

Add one long-lived **observation module** with a narrow interface:

```text
start(connection settings, account)
stop()
subscribe(verified state / change notices / health)
```

Its IBKR adapter owns one observation-only, nonzero-client socket and reader
loop, using a configurable dedicated client ID distinct from the existing
capture/writer client ID. Validate both IDs before connecting and fail visibly
if the observer ID is occupied; do not silently take client ID 0 or reassign
the writer's ID. Never connect two sockets with the same client ID at once.
Use an in-process incrementing observer generation to reject callbacks from
an old socket after reconnect or settings changes. A timestamp is unnecessary.
The initial observer need only subscribe to positions and connection health;
visibility of app-owned order or execution callbacks on this separate ID must not be
assumed. “Observation-only” describes the adapter's allowed calls; the TWS
Read-Only setting remains whatever the separately configured paper mode
requires.
Callbacks are converted to immutable, account-scoped hints on a queue; the
callback thread never changes GUI state or writes to the journal. A single
reconciliation worker coalesces hints, serializes broker reads, and publishes
only fully verified `PortfolioSnapshot`/`BrokerSnapshot` values through the
existing coordinator rules. Keep this module independent of the GUI. Use an
in-memory fake at the same interface for deterministic callback replay.

Initially, keep the existing bounded snapshot adapter as the source of truth.
The observer's persistent socket is for position/health change detection; it
schedules the same coherent reads already used by Refresh. This avoids turning
an individual callback into a partial planning snapshot. The existing writer
can also signal the worker after its own acknowledgements. Detect app-owned
order changes from verified repeated snapshots unless paper testing proves a
safe order callback subscription on the observer's client ID. If simultaneous
TWS connections prove unreliable, move capture requests onto the persistent
socket in a later slice, preserving the current completion barriers and
read-only inspection semantics. Do not expose callback-maintained mutable
state to the planner.
The capture connection may later be replaced by bounded reads on a persistent
socket. Confirmation must still perform fresh, complete broker verification
immediately before a paper send; connection health and callback arrival alone
do not establish current position and order state.

Do not require a Master Client ID or change TWS's Master Client configuration
for the first release. If the dedicated ID does not receive fills or app-owned
order callbacks, detect changes through position updates, writer callbacks,
and bounded `reqExecutions`/`reqCompletedOrders` reads in the existing selected
snapshot. If history is unavailable, report the fill cause as unknown; never
infer it solely from a smaller position or a vanished order.

The worker performs an initial complete inventory without alerts. On an
available position/execution/order hint it invalidates any affected displayed
snapshot immediately, then requests a complete observation. For manual or
other-client orders, it also performs bounded periodic `reqAllOpenOrders`
reconciliation while connected; a position callback often coincides with a
fill, but it cannot detect an untouched bracket price edit by itself. Start
with a 15-second order reconciliation interval, coalesce bursts into one read,
and make that interval configurable for paper testing. Allow only one complete
capture at a time, including manual Refresh and pre-write reads; bound queue
growth, use backoff on failures, and avoid repeating selected-contract quote,
contract-detail, and market-rule reads when only the order inventory needs
inspection. Do not silently extend a snapshot's TTL because a lightweight
order read succeeded. Track overlapping
requests and use request/connection epochs so late callbacks cannot publish
into a newer capture. A failed or timed-out read leaves the UI stale and
retries with backoff; it does not restore the last snapshot as actionable.

No UI request should wait on the observer's socket. Publish state changes to
the local StarUI page through a loopback-only Server-Sent Events endpoint (or
the framework's equivalent push channel). The event contains a revision and
minimal change description; the browser fetches the latest rendered state.
Reconnect the page stream without replaying old toast alerts. Re-rendering
must preserve unsubmitted form inputs and drafts; a broker update that changes
their validity disarms any pending confirmation and asks for review. Close the
observer and page stream cleanly when the window closes or settings change.
The event stream is display-only and loopback/session protected like the
existing page routes; it never invokes a trading action. A dropped browser
stream may leave the display stale, so show stream health and fetch the latest
revision after reconnect.
For a newly opened position, update only the inventory region in the browser.
Keep the editor and selected contract mounted. Clear that position's **NEW**
badge when the user selects it. An initial inventory is a baseline and earns
no badges. Badge detection uses account and contract ID, not row order. A
contract that first appears unverified receives **NEW** when a later complete
read establishes eligibility. A same-account Refresh or reconnect compares
with the last verified inventory; changing accounts starts a new baseline.
When the verified quantity of the selected contract changes, keep its editor
mounted and show the net number of contracts added or removed since the last
view update. Offer an explicit **Update view** action. The server captures the
new broker state immediately and disarms pending confirmations; the browser
retains unsubmitted form fields until the operator updates the view. If a
decrease makes a draft allocation invalid, retain it for review while planning
remains blocked. The notice describes a position quantity change, not a proven
fill or trade cause.

## State and notification rules

Compute differences only between two **complete, verified observations** of
the same account. Key positions by `(account, conId)`, orders by account and
positive permanent ID, and app-owned bracket legs additionally by the exact
journal-proven OCA group and expected IDs. Canonicalize ordering, deduplicate
`execId` values and identical status callbacks, and show a single notice for a
burst of related changes. Never compare across account changes or connection
epochs as if they were ordinary trades. The first good observation after
startup establishes a baseline. After a reconnect, a same-account **NEW**
badge may identify a contract absent from the last verified inventory, but it
does not assert when or why that position opened. Fill or order alerts still
need their own completed reconciliation.

| Confirmed change | UI result | Alert policy |
| --- | --- | --- |
| New nonzero long option position | Add to portfolio; retain current selection unless it vanished | One in-app notice, “New option position available”; select it only on user action |
| Selected quantity changed | Update broker state and sidebar, disarm confirmations, keep draft inputs, and offer **Update view**; block invalid allocations | Persistent count of contracts added or removed, without claiming a fill cause |
| Basis changed | Update selected planning values and disarm affected confirmations | Broker state update; execution attribution awaits reconciliation |
| App-owned OCA pair appears | Keep pending journal state until both exact legs and their states are verified | One “Both bracket legs observed in TWS” notice only after existing reconciliation proves the pair; otherwise “Pending TWS verification” |
| Manual/other-client bracket appears | Show as external protection/reservation, inspect-only | One informational notice after a complete order read; never offer manage actions |
| Bracket price, quantity, status, or OCA pairing changes | Update active rows and reservation, invalidate affected reviews | Flag changed row; alert if protection becomes incomplete, disappears, or fills |
| One or both legs disappear | Reconcile with completed orders, executions, positions, and journal | Show “Protection needs review” until cause is proven; never equate disappearance with fill/cancel |
| Fill/partial fill | Show known execution and remaining quantity only after reconciliation | Alert once per materially new fill; distinguish partial from complete |
| Disconnection, lost broker data, timeout, wrong account, or safety-setting mismatch | Mark all broker data stale and block affected actions immediately | Persistent status banner; one transition notice, no repeated toast storm |

The word “transmitted” needs care: a local `transmit=True` request or an API
acknowledgement proves only what was sent or acknowledged. A complete,
journal-proven pair with both legs observed in `Submitted` or `PreSubmitted`
meets the existing reconciliation rule for an active layer, but the alert
should say **both legs observed in TWS**. That does **not** prove exchange
acceptance, a triggered stop, or that TWS did not hold an order for a
precaution. Other combinations, including `PendingSubmit`, rejected,
untransmitted, and incomplete pairs, retain explicit pending/unknown labels.
If the product later needs a stronger “transmitted” claim, first capture and
validate the relevant `orderState` and `whyHeld` fields and establish the exact
meaning in paper TWS; current `CapturedOrder` drops those fields. TWS order
precautions remain in force.

Potential additional alerts worth including: unexpected loss of one protective
leg, a rejected/held order, position closure while an exit order remains,
account/connection switching, and data stale beyond its freshness limit.
These are higher priority than routine price-edit notices because they affect
the operator's understanding of protection and available quantity.

## Safety invariants

1. The observation module has no order-write or order-binding methods. It
   never modifies or cancels manual/other-client orders.
2. Callback hints do not make a broker snapshot `READY`. Existing completion
   barriers, account and exact contract identity checks, and TTL still apply.
3. A callback or periodic read during an armed action disarms its confirmation
   if relevant broker state changes or becomes uncertain. The disarm happens
   before any refreshed UI can offer Confirm. Confirmation still makes its own
   fresh read and compares its plan fingerprint. Serialize background reads
   with existing write verification so a late background result cannot replace
   the result of a newer pre-write or post-write capture.
4. Partial fills, missing execution history, order disappearance, disconnects,
   callback errors, and ambiguous OCA pairing fail closed. Alert text must
   describe uncertainty rather than assert a sale or successful protection.
5. Event processing is idempotent across duplicate callbacks, reconnects, UI
   reloads, and restarts. The existing journal remains the authority for app
   ownership; observing an order does not confer ownership.
6. The observer obeys the configured paper account, loopback host, dedicated
   nonzero client ID, and TWS safety setting. Paper execution remains
   separately enabled and unchanged.

The project's recorded paper-TWS probe already observed a manual transmitted
order through `reqAllOpenOrders()` with `readOnlyApi=true` on TWS 10.50.1e/API
10.45.1. This supports the nonbinding snapshot path for that version pair.
IBKR's older setup guide says order information may be unavailable with
Read-Only enabled, so every targeted version must still pass the manual-order
visibility gate before this feature is presented as reliable. See
[the compatibility record](compatibility/TWS_10_50_1e_API_10_45_1.md) and
[source research](research/TWS_EVENT_CALLBACKS.md).

## Implementation slices

1. **Position observer:** persistent read-only subscription, connection
   health, verified portfolio reconciliation, new-position notice, and UI push.
   Preserve manual Refresh. No order or journal changes.
2. **Order reconciliation:** periodic nonbinding `reqAllOpenOrders`, selected
   contract reads on relevant hints, pure snapshot diff, external-order labels,
   bracket price/quantity/status changes, and protection warnings.
3. **Execution and journal integration:** use execution callbacks when visible
   and bounded execution/completed-order reads as the fallback; reconcile
   commission reports, distinguish partial/full fills, and promote pending app
   brackets only through existing journal checks.
4. **Recovery and hardening:** reconnect/resubscribe, lost-data codes,
   callback/capture races, startup/restart baselines, alert deduplication,
   window shutdown, and paper-TWS compatibility evidence.

## Verification and acceptance

Run `.venv/bin/python -m pytest -q`, `.venv/bin/python -m mypy`, and
`.venv/bin/python -m ruff check src tests`. Add deterministic callback-replay
tests for startup baseline, new position, zero/closed position, partial fill,
duplicate/out-of-order events, manual and app-owned bracket edits, missing leg,
rejection, disconnect, reconnect, restart, overlapping reads, and user edits
made in TWS. Test that no observation path calls `placeOrder`, `cancelOrder`,
`reqAutoOpenOrders`, client-0 binding, or `reqGlobalCancel`. Test that an armed
action is invalidated when relevant state changes and that confirmed writes
still require their own fresh snapshot. Include an observer client-ID collision,
missing execution callback, changed account/settings, failed order read,
browser stream reconnect, and background read racing a pre-write capture.

In paper TWS, manually open an option and verify its position appears and the
app alerts without pressing Refresh. Create, edit, partially fill, and cancel
manual brackets; verify the app detects them while keeping them inspect-only.
Submit an app-owned paper bracket and test both the normal working state and a
TWS precaution that delays transmission. Disconnect/reconnect TWS and restart
the app, checking stale banners, resubscription, no duplicate alerts, and
correct journal reconciliation. Confirm two simultaneous, distinct nonzero
client IDs do not disrupt captures or writers. Record observed latency and any
client-ID visibility gaps. Accept a new-position alert only after a verified
portfolio update, no startup toast for existing positions, and no false
“both legs observed” notice for a held or incomplete bracket. Manual bracket
edits should appear within the configured reconciliation interval plus one
bounded read; if that target cannot be met, report the measured limit in the UI and release
notes. Paper results do not establish live behavior.

## Product decisions

- Show in-app alerts only, including when the app window is in the background;
  no operating-system notification in this release.
- Quietly update and flag bracket price edits. Alert on fills or missing
  protection, rather than interrupting for every edit.
- Keep the current selection when a new position arrives. Selecting the new
  contract is a deliberate user action because selection affects drafts and
  action context.

## Relevant existing code

- `src/ibkr_options_manager/broker/ibkr.py`: bounded capture and callbacks.
- `src/ibkr_options_manager/broker/read_only.py`: capture data and transport
  interfaces.
- `src/ibkr_options_manager/portfolio.py` and
  `src/ibkr_options_manager/snapshot.py`: verification and publication.
- `src/ibkr_options_manager/app/web/surface.py`: refresh, toasts, action
  disarming, and journal reconciliation.
- `docs/SNAPSHOTS.md` and `docs/PAPER_EXECUTION.md`: current safety contracts.
