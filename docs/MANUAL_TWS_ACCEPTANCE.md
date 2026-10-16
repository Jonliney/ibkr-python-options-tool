# Manual paper-TWS acceptance suite

Prepared 2026-10-16 against the current source tree. This is an executable
acceptance runbook, not a record of tests already passed. No orders were placed
while preparing it. Record the Git commit and any local changes for each run.

The objective is to test the actual desktop app, TWS, broker callbacks,
precautions, order lifecycle and operator recovery together. Offline tests
already cover most arithmetic and validation rules. Repeating every unit-test
input manually adds little; the cases here target evidence those tests cannot
provide: real acceptance, fills, OCA behavior, callback timing, persistence,
embedded-browser interaction and the accuracy of what the operator sees.

Passing this suite establishes coverage of the listed behaviors in the tested
paper environment. It cannot establish that the entire platform is free of
defects or certify live execution. IBKR simulates stops and complex orders in
paper, uses top-of-book fills, and documents special partial-market-fill
behavior. See [IBKR paper limitations](https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm).

## 1. Execution rules and evidence

Use a dedicated **paper** account. Verify both TWS's simulated-trading indicator
and the intended `DU…` account before every group of cases. All purchases below
are paper test fixtures, entered manually in TWS. The app manages long exits;
it does not place the opening buys. Never run this checklist against live funds.

Start with one app instance and no unrelated orders on the test contract.
Keep TWS order precautions enabled. Do not enable auto-transmit or disable a
precaution to make a test pass. If TWS requests Transmit, inspect each leg and
transmit only the correct reviewed paper order; record that intervention.

Suggested launch, replacing the placeholder with your actual paper account:

```sh
.venv/bin/ibkr-options-manager-gui --account DU1234567 --enable-paper-execution
```

Current defaults are loopback, port 7497, execution/snapshot client 17 and
position observer client 18. Check your actual TWS configuration rather than
assuming its port. API read-only must be disabled for the positive execution
cases; explicitly enable it for the read-only negative case. Preserve the
client ID and journal between normal restarts.

In TWS show Orders, Trades/Executions and Portfolio together. Expose account,
contract/local symbol, action, total/filled/remaining quantity, order type,
limit, stop, trail amount/percent, limit offset, TIF, status, OCA group,
Outside RTH, order ID, permanent ID and client ID wherever available. Use
order details or API diagnostics for fields absent from the grid.

For **every** case:

1. Record the fixture, starting position, all working exits and latest fills.
2. Capture the app's action review **before** confirmation.
3. Perform only the listed action; inspect both TWS and the app afterward.
4. Record every new/cancelled/amended order, final quantity and any warnings.
5. Refresh, then check the same state again. For a failure, preserve evidence
   before cleanup. Do not count a toast or vanished row as broker proof.
6. Run the cleanup protocol below before buying the next fixture.

Use `PASS`, `FAIL`, `BLOCKED`, `NOT RUN`, or `NOT OBSERVED`. If the intended race
or partial fill did not occur, mark `NOT OBSERVED`, even if the happy path worked.
An expected rejection is a PASS only when the app handles it correctly.

**Stop the run** on a duplicate sell, negative position, unrelated-order change,
unexplained surviving sibling, falsely reported success, or permitted retry
after uncertainty. Inspect the actual account before continuing.

## 2. Contracts and reusable fixtures

Do not hard-code today's strike or conId into a reusable test. On execution day,
choose a listed expiry **7–21 calendar days out**, a near-the-money strike with
a positive two-sided quote, and enough paper buying power. Avoid expiry day.
Use a paper BUY LMT at the displayed ask, inspect the preview, then wait for
the **entire** requested quantity to fill. Reprice deliberately if necessary.
Cancel any unfilled opening remainder before the app creates exits. An open
BUY is itself a negative test, not a clean starting fixture.

| Alias | Select in TWS | Purpose |
| --- | --- | --- |
| C | SPX call, preferably a verified SPXW PM-settled series | Primary index-option path |
| P | SPX put, same expiry and nearby strike | Right/identity isolation |
| N | Another SPX call: same expiry, adjacent strike | Same-symbol/different-conId isolation |
| E | SPY call, 7–21 days out, near the money | Equity/ETF-option and Outside RTH contrast |
| X | XSP call, 7–21 days out, near the money | Second index-option contract and market-rule check |

Record account, conId, security type, expiry, strike, right, multiplier,
currency, trading class, local symbol and routing exchange. C and P are not
interchangeable. A standard SPX and SPXW series must not be treated as the same
contract. Verify the actual series in TWS; Cboe lists distinct settlement and
expiration conventions in its [SPX specifications](https://www.cboe.com/tradable_products/sp_500/spx_weekly_options/specifications).

Use **one fixture at a time**, unless a case explicitly requires simultaneous
contracts. Quantities below are starting held quantities, not additional buys
on top of an earlier case. Reuse an existing position only after reconciling
its full working-order and fill state.

| Fixture | Paper holding | App draft allocation |
| --- | --- | --- |
| C1 | Buy 1 C | 1 |
| C4 | Buy 4 C | 1 / 1 / 1 / 1 |
| C5 | Buy 5 C | 2 / 1 / 1 / 1 |
| C6 | Buy 6 C | 1 / 1 / 1 / 1 / 1 / 1 |
| C7 | Buy 7 C | Either no draft, or 2 / 2 / 2 with 1 unassigned |
| C10 | Buy 10 C | External reservation 5; app 2 / 1 / 1, leaving 1 unassigned |
| E2 / X2 / P2 | Buy 2 of E / X / P | 1 / 1 |

### Price recipe

Let `B` be the app's verified **premium per unit** entry basis, `b` the current
bid, and `m` the verified multiplier. Total premium basis is `B × m × quantity`.
Compare this with TWS's basis convention; do not multiply a total-contract cost
by the multiplier again.

For ordinary four-layer cases configure targets at **+20%, +40%, +60%, +80%**
from B and stops at **−25% return**. For six layers use +20% through +120% in
20-point steps. Before sending, confirm the rounded targets are above the
current ask and stops below the current bid. If the market has already crossed
them, reset the fixture or explicitly choose wider values and record them.
The default recipe must not accidentally turn a non-fill case into a fill case.

Use the selected contract's fetched market-rule increments, including price
bands. Never assume SPX, XSP and SPY have the same tick. New bracket targets and
stop triggers round up; STP LMT limits round down. Initial trailing stops round
down and trailing limit gaps round up. Compare the reviewed **rounded prices**
with TWS, rather than comparing percentage text alone.

The app's dollar **trail distance and trailing limit offset** fields are money
per contract: a $100 distance with verified multiplier 100 means 1.00 quoted
premium unit. Percentage trails refer to option premium, not the SPX index.
Other dollar inputs must be checked against their own labels/review; do not
assume every price editor uses trailing-dollar semantics.

For each successful bracket submission, the expected TWS state is two closing
SELL orders per layer: one LMT target and one STP or STP LMT protective leg,
GTC, same quantity and unique OCA group within the pair, OCA type 2. Four
layers mean **8 orders, reserving 4 contracts**, not 8 contracts. These are
exit OCA pairs on an existing holding, not a new BUY parent with children.
IBKR defines type 2 as proportional reduction with block in its
[order reference](https://www.interactivebrokers.com/docs/tws-api/ref/order).

## 3. Core journeys: execute these first

Each numbered ID is independently reportable. Steps within a case are sequential.

### J01 — Four brackets → profitable trailing stop → close

1. Prepare C4. Build four layers of 1 using the price recipe and STP protection.
2. Edit a price locally. Verify TWS is unchanged. Cancel the edit, then restore
   the intended draft. Review, inspect all four pairs, and Confirm.
3. Inspect TWS for 8 SELL orders, four distinct OCA groups, correct prices,
   quantity 1 per leg, GTC and correct contract. Check any Transmit holds.
   Refresh until the app verifies manageable active pairs; a pending row is
   not yet a successful reconciliation.
4. If bid rises above B before a target fills, choose Set a trailing exit with
   a **10% trail and no limit offset**. Check the reviewed trigger is below
   fresh bid. Review and Confirm.
5. In TWS verify all original 8 orders are terminal cancelled, none working,
   and exactly **one SELL TRAIL, quantity 4, GTC** exists. No new MKT exists.
   Verify trailingPercent and initial stop against the final reviewed values.
6. Observe a favorable premium move and then a reversal. TWS's trailing stop
   should ratchet up on the favorable move and not move down merely because
   premium falls. If it fills, verify 4 total SELL executions, flat position,
   no working trail or old bracket legs, and recorded fill/P&L evidence.
7. If no reversal occurs, use Exit position now: trail cancellation must precede
   one SELL MKT for the current held quantity. Verify flatness and no exits.

Profit is a condition to observe, not something the tester can force. If it
does not occur, run the conversion at the current premium to test mechanics
and separately mark the profitable conversion/ratchet observation NOT OBSERVED.
If a target fills first, do not pretend the quantity is still 4: use J07 or
reset C4. Never manufacture an app-owned fill by manually repricing its leg
and then call it an unchanged happy path.

### J02 — Buy seven, exit immediately without brackets

**Current-build UI gap:** `_workspace`'s toolbar rendering in
`app/web/surface.py` only includes the position-exit control when active pairs
or a working trail exist. The service has a no-exit path, covered by
`test_position_market_exit_covers_unassigned_and_suppresses_restart_duplicate`,
but that does not make this journey accessible in the desktop. Mark J02
**BLOCKED — no unbracketed-position exit control** on this build. Do not buy
seven just to discover that the button is absent. The steps below are the
acceptance criteria when the control becomes exposed; J03 is runnable now.

1. Prepare C7 with no exits and no drafts. Choose **Exit position now**.
2. Review must show zero cancellations and **one SELL MKT for 7**, GTC.
   The bid is an estimate, not an execution-price promise.
3. Confirm once. TWS must show one exit, not seven orders. Verify the actual
   fill quantity/price, flat position and absence of remaining SELL orders.
4. Refresh, click again if a control remains available, and restart the app.
   No duplicate order may be sent. Inspect the MKT history and actual fills.

### J03 — Whole-position exit includes unassigned contracts

1. Prepare C7. Submit three brackets of **2 / 2 / 2**, leaving **1 unassigned**.
2. Choose Exit position now; review must cancel 6 legs and sell **7**, not 6.
3. Confirm. Verify all pairs cancelled before one MKT 7 is submitted, then
   flat position and no old exits. Any position change during cancellation
   requires a stop/review rather than a sell based on the original quantity.

### J04 — Sell one layer, then sell the remainder

1. Prepare C5 with layers 2 / 1 / 1 / 1.
2. Use the quantity-2 row's Sell now action, not the position toolbar.
3. Review must cancel only that pair and place one MKT **2**. After fill,
   TWS holds **3**, with exactly 6 unchanged legs for the other three groups.
4. Inspect realised history, then Exit position now. It must cancel those
   6 legs and sell the remaining **3** once. End flat with no exits.

### J05 — Delete one and delete all keep the holding

1. Prepare C4 with four brackets. Delete the second layer through review and
   confirmation. Verify only its two legs cancel, position remains **4**,
   and 6 legs remain. No MKT/TRAIL may appear.
2. Add a new bracket for the newly unreserved 1. Verify a new group/IDs,
   with the other three pairs unchanged.
3. Cancel all exits. Verify all active owned legs cancelled, still holding 4,
   and no replacement sell. Refresh and inspect unreserved quantity 4.
4. Rebuild the same four-layer draft. Verify it creates fresh attempt/OCA
   identities rather than adopting the old cancelled orders. Clean up.

### J06 — Target fill cancels only its sibling

1. Prepare C4 with the first target just above current ask on a valid tick;
   keep the other targets farther away. Review and submit normally.
2. Wait for a natural premium rise to fill that first target. Record the
   actual execution, sibling cancellation and their timing.
3. Expect held quantity **3**, three intact other pairs, no surviving first
   stop and one sold history row. Refresh/restart; no cancelled sibling may
   return and no filled layer may be offered for a second sell.
4. If no target fills within the test session, mark the fill branch NOT
   OBSERVED, then clean up. Do not infer fill behavior from acceptance alone.

### J07 — Convert after one layer has filled

1. Continue J06 only after its exact quantity-1 fill is verified.
2. Set a trailing exit, 10%, no limit. Review must cancel only the remaining
   3 pairs and trail **3**. Confirm and verify one TRAIL 3, no working pairs.
3. Verify realised P&L from the first layer stays separate from the remaining
   trail projection. Exit/observe trail closure and reconcile total fills 4.

### J08 — Stop fill and OCA sibling behavior

1. Fresh C4: set the first stop one or a few valid ticks below bid, other
   stops farther away, targets safely above ask. Submit only while the first
   stop remains below bid; otherwise reset the review.
2. Wait for a decline to trigger it. Verify that stop's actual SELL fill and
   target cancellation, remaining position 3 and other pairs unchanged.
3. If more than one stop is crossed, account for every fill and expected
   remaining pair; do not insist on 3 if market movement filled other layers.
4. Check stop execution price versus trigger without requiring equality.
   A triggered stop is not a guaranteed-price exit. Clean up the remainder.

### J09 — Uneven quantities and six-layer submission

1. Fresh C5: Build Draft with four target presets. Verify allocation **2/1/1/1**
   if the current build distributes the remainder to earlier rows; inspect
   totals and adjust explicitly if using a different allocation control.
2. Submit; verify 8 legs reserving 5 and both legs of the first pair quantity 2.
   Cancel all, verify position remains 5, then close it.
3. Fresh C6: configure six presets, build six quantity-1 layers and submit.
   Verify 12 matching legs, six groups, no missing final layer or hidden
   timeout. Record total duration, holds and per-stage trace timing.
4. A timeout after some legs is an uncertain partial result, not a successful
   six-layer test. Follow failure recovery; never press submit repeatedly.

### J10 — Stop-limit creation, edits and non-fill

1. Fresh C4: choose STP LMT with a **5% limit offset**. Submit four layers.
   Verify each protective order is one STP LMT, with separate trigger and lower
   limit, plus its target. Expect 8 orders, not separate stop and limit legs.
2. Amend one target. Verify only its app-owned target changes, preserving IDs,
   quantity, group, TIF and stop-limit prices.
3. Amend one stop using its saved offset rule. Review must show both trigger
   and recomputed lower limit; verify both TWS fields and the same stop IDs.
4. Set all active stops to a valid common trigger below bid; retain the offset.
   Verify every affected trigger and limit, no target changes. Repeat with
   an explicitly changed common offset rule and inspect every resulting limit.
5. In a fresh C1 STP LMT case, put the trigger close below bid and a narrow
   valid limit gap. Observe a trigger if the market declines. It may fill or
   remain working after a gap; record which. A trigger without execution must
   not appear as a completed sale. Gap/non-fill is conditional, not guaranteed.

### J11 — Break-even and common stop edits

1. Fresh C4 STP pairs: edit one target and one stop independently. Verify review
   values versus TWS and preservation of unrelated legs. Test Cancel changes
   before confirming another edit; TWS must remain unchanged for that edit.
2. When bid is above the rounded entry trigger, Raise stops to B/E. Verify
   reviewed triggers use full verified B and tick rounding, not the rounded
   cost text. Targets, IDs, quantities and groups stay unchanged.
3. Raise one stop above B but below bid. Raise stops to B/E again: the higher
   stop must not be lowered. Inspect all four actual triggers.
4. Set all active stops to **−20% return from B**, then to **0%** where valid.
   Check the same premium trigger on every eligible stop.
5. Separately stage a stop at/above current bid. Verify the immediate-sell
   warning and cancel the proposal. This is not automatically an invalid
   bracket price if it is positive and below target. No write occurs on Cancel.

### J12 — Draft controls stay local

1. Fresh C4 with no exits: Add Layer, remove it, then Build Draft. No order
   may appear in TWS. Build Draft again must not silently replace existing edits.
2. Test Set all draft stops, Move all draft stops to B/E, STP/STP LMT selection
   and per-layer prices. Verify only draft fields change.
3. Submit only two quantity-1 layers. Expect 4 working legs and **2 unassigned**,
   not an implicit four-contract submission. Cancel and close explicitly.

### J13 — Split assigned versus all available

1. Fresh C7: create three draft rows and explicitly assign **1/1/1**, leaving 4
   unassigned. Choose **Split assigned**: total remains 3, distributed 1/1/1.
2. Choose **Split all available**: total becomes 7, distributed **3/2/2** with
   the remainder on earlier rows. Remove a row, add one, and inspect the new
   allocation; nothing is sent to TWS by any split/add/remove operation.
3. Repeat beside a verified external SELL LMT reservation of 2: all available
   is **5**, not 7; three rows should split **2/2/1**. Submission must not
   exceed that unreserved balance. Cancel the external fixture manually before
   final cleanup.

## 4. Trailing orders: rules, units and toolbar behavior

Use fresh C4 and J01's accepted four pairs for each independent conversion.
Choose distances/gaps that leave a strictly positive stop and limit; use 10%
trail and 5% gap as starting values, inspecting current quotes first.

| ID | Manual actions | Required TWS/app evidence |
| --- | --- | --- |
| T01 | Convert four pairs to TRAIL LIMIT using 10% trail, 5% limit gap. | 8 old legs cancelled; one SELL TRAIL LIMIT 4/GTC. Percent trail and initial stop match review. Gap is one fixed rounded quoted-price offset derived from the initial stop, not a percentage that keeps changing. |
| T02 | Fresh conversion using $100 trail distance and $25 gap, if m=100 and premium permits; otherwise record an explicit valid scaled amount. | Quoted distances 1.00 and 0.25 when tick-valid; review shows effective rounded gap. No factor-of-100 error in TWS. |
| T03 | On a working trail, change percentage distance to a valid dollar distance; then dollar to percent. | Same order/permanent IDs, quantity and GTC; correct active unit. Old auxPrice/trailingPercent must not remain an unintended second active rule. Compare order details after each confirmed edit. |
| T04 | Edit only the limit gap after a favorable move has ratcheted the stop. | Review retains the observed ratcheted stop; amendment does not reset it from entry/old bid. Correct fixed gap in TWS. Mark ratchet branch NOT OBSERVED if no move occurs. |
| T05 | Remove offset to switch TRAIL LIMIT → TRAIL; add valid offset to switch back. | Same IDs, correct new type, stop/rule/quantity retained as reviewed. A TWS rejection is recorded as uncertain, locks management, and never looks like successful conversion. |
| T06 | Use Set all stops on the working trail with a premium trigger below fresh bid; then Raise stops to B/E when feasible. | Review derives dollar distance from fresh bid, retains explicit trigger and existing gap. B/E never lowers an already higher stop. A limit below basis is not labelled a guaranteed break-even fill. |
| T07 | In Set all stops on a trail, enter a trigger equal to bid and above bid; repeat with unavailable bid if reproducible. | Apply blocked with explanation; existing inputs retained; TWS unchanged. This trail control differs from the bracket warning case in J11. |
| T08 | Delete trail, then separately repeat with toolbar Cancel all exits. | Exactly the owned trail cancelled; held 4 retained; no replacement. Refreshed cancellation/history state persists across restart; hiding a cancelled row sends nothing. |
| T09 | Fresh working trail: Exit position now. | Trail cancellation verified first, then one MKT 4; flat after actual fills. No overlapping working trail and replacement sell. |
| T10 | Fresh C7 with only 3 quantity-1 brackets; convert available position. | Cancels 6 legs and creates one TRAIL 7, including 4 unassigned. No manual override silently reduces quantity to 3. |
| T11 | After a trail exists, try to stage another for the same contract. | No second overlapping trail; unsupported overlapping management is blocked. Inspect actual order inventory, not only button appearance. |

## 5. External orders and contract isolation

Create external orders **manually in TWS**, record their IDs and groups before
the app acts, and keep their limits far above ask so they do not fill during
non-fill cases. External orders must remain inspect-only even if they resemble
the app's order structure. Do not copy app ownership identifiers.

| ID | Setup and actions | Expected result |
| --- | --- | --- |
| O01 | C10: manual standalone SELL LMT 5; app brackets 2/1/1 (4 total), leaving 1 unassigned. Convert to trail. | External LMT 5 unchanged; app's 6 legs cancelled; one trail **5**, not 4 or 10. Position 10; total closing reservation 10. |
| O02 | Fresh C10: replace external standalone with a valid manually created LMT/STP OCA pair for 5; repeat O01. | External pair reserves 5 once; both external legs unchanged; resulting app trail 5. |
| O03 | With O01/O02 external orders present, choose Exit position now or try external row management. | Whole-position exit blocked; app cannot cancel/adopt external orders. No new MKT 10 alongside external reservation. |
| O04 | Fresh C4: manual SELL LMT 4; attempt Build Draft/new bracket. Then change it to 3 in TWS and refresh. | Initially zero available and no send. After verified reduction, only 1 available. No takeover of external order. |
| O05 | Fresh C4: create an external OCA group with unequal leg quantities (e.g. target 2, stop 1), or a third leg, while all orders remain nonmarketable. | Ambiguity blocks new exits and conversion; no guessed reservation or external cleanup. Remove malformed orders manually before cleanup. |
| O06 | C4: leave an opening BUY LMT 1 below market working while staging exits. | New bracket/trailing actions fail closed on opening-order conflict. Cancel the BUY manually, refresh, then review anew. |
| O07 | Hold 2 C, 2 P and 2 N together; put an external nonmarketable exit on N. Submit/manage two layers on C, then P. | Correct conId/right/strike in every review and TWS order. Each action leaves other contracts, quantities and orders unchanged. Selection changes cannot reuse another contract's confirmation. |
| O08 | E2 then X2: create two brackets, edit stops, cancel, and close; inspect both instruments independently. | Verified multiplier/cost/tick rule and identity each time. New index-family bracket Outside RTH setting follows supported routing; E's default is false. Do not assume SPX tick sizes on either fixture. |
| O09 | If a non-USD option is already available in paper, inspect it and attempt trailing conversion. Do not buy one solely to obtain this state. | USD-only trailing limitation is explained and no trail is sent. Otherwise mark BLOCKED by missing fixture. |

## 6. Stale reviews, manual TWS interference and duplicate actions

Use C4 with four verified pairs unless specified. Perform interference **after
fresh Review but before Confirm**, so this tests the broker-state check rather
than just initial display. Screenshot original and changed state.

| ID | TWS/operator action | Expected result |
| --- | --- | --- |
| R01 | Review a draft submission, price amendment, cancel, market exit and trail change in turn; wait at least 11 seconds, then try Confirm. | Ten-second server deadline blocks each expired action. Re-review required. TWS unchanged. Record each action family separately. |
| R02 | Review one active stop edit. Manually change that exact stop price in TWS before Confirm. Repeat with a target edit. | Stale confirmation blocked; no overwrite of the manual price. Refresh explains/reconciles conflict; management requires a supported new review. |
| R03 | Review Exit position now/trailing conversion. Change a different reviewed layer's quantity or price in TWS. | Whole reviewed set mismatch blocks continuation, even if edited layer is not first in the sequence. |
| R04 | C10/O01: review conversion; change the retained external order's price but keep reserved quantity 5. | Conversion blocks despite unchanged reservation total. External order remains untouched. |
| R05 | Review an action; buy 1 more C in TWS and wait for its fill, changing quantity and basis. | Old confirmation cannot act on previous quantity/basis. Inventory offers observed update without silently approving old draft. Fresh valid plan required. |
| R06 | Fresh C4: after review, manually sell 1 in TWS while app exits still exist, solely in paper. Immediately inspect/reconcile all outstanding exits. | Quantity/fill conflict blocks app continuation. It must not sell original 4 after only 3 remain or silently cancel external orders. This intentionally creates excess reservation; use cleanup before any further sale. |
| R07 | Review a no-exit C7 MKT exit; double-click Confirm, press Enter repeatedly, then Refresh. | At most one MKT 7. Repeat with C4 bracket submission: at most 8 legs/4 groups. Count TWS permanent IDs/executions. |
| R08 | While editing C, switch to P or open Settings; return and inspect the review. | No confirmation applies C's data to P. Unsaved input handling is clear; selected identity remains visible. |
| R09 | Manually cancel both legs of one app pair in TWS without changing position. Wait for observation; then Refresh if needed. | App does not sell or recreate the missing pair. Requires terminal evidence or explicit broker-checked Verify; other pairs remain intact. |
| R10 | Manually cancel only the stop, leaving target working. Refresh, dismiss/reopen warning, restart app. | One-sided state remains blocked and visible; no auto-recreation, adoption, blanket cancellation or retry. Operator confirmation cannot clear it while target survives. |
| R11 | Manually change an app order's GTC to DAY if TWS permits. | Management fails closed on policy mismatch. No automatic rewrite to GTC. If TWS disallows the edit, record that boundary. |
| R12 | Repeat submission in two app instances sharing the same paper journal and execution client ID. Record whether TWS refuses the second client first. | No duplicate orders or lost journal entries. Connection/client contention may safely block the second app. A client-ID rejection does not prove journal-lock contention; that is covered offline/injection. |

## 7. Failure and recovery drills

These cases intentionally leave uncertain results. A stop after partial
cancellation can leave part of the holding without protection. The acceptable
result is a truthful count, no unsafe next write and a durable recovery path,
not an all-or-nothing rollback promise.

### F01 — Connection unavailable and read-only gates

1. Launch without TWS running. Verify usable retry/settings controls, no false
   empty-portfolio success and no enabled execution from cached data.
2. Start paper TWS, retry, verify exact account and positions.
3. Launch normally **without** `--enable-paper-execution`; preview C1. No
   action may transmit or amend/cancel an order.
4. Relaunch with the flag but TWS API read-only enabled. Attempt submission;
   require a blocked action and no working orders. Restore the setting only
   after ending the negative case and inspect a fresh review.
5. Try empty/malformed/non-managed account text in Settings. No write or
   accidental fallback to another managed account. Do not log in to live TWS
   to test live rejection; the account gate is covered offline.

### F02 — Disconnect before confirmation

Fresh C4: review a submission or active action, then disconnect TWS before
Confirm. Require failed fresh verification and no new write. Reconnect; the
indicator and inventory recover, but old confirmation must not silently resume.
Repeat for new brackets, price edit, cancel, trail conversion and position exit.
Use separate observations for each; one family passing does not prove all five.

### F03 — Cancellation interrupted during conversion/exit

1. Fresh C4, 8 verified legs. Start a trailing conversion. Watch TWS. After
   the first pair cancels, disconnect TWS's API/session before the rest finish.
   A second operator may make this easier; do not automate timed order writes.
2. Record exactly which permanent IDs cancelled and which still work. The
   app must stop on the first detected incomplete read/cancel, report confirmed
   progress, and not claim a completed four-contract trail.
3. If final submission was not reached, **no replacement TRAIL/MKT should exist**.
   If the interruption landed after a send, the honest result is unconfirmed;
   inspect TWS for a possible replacement rather than assuming it was absent.
4. Reconnect, Refresh, restart. No automatic resume or duplicate replacement.
   Surviving orders must be visible; no uncertain cancellation may be called
   confirmed without exact broker evidence.
5. Repeat independently with Exit position now and Cancel all exits. A partial
   cancel-all leaves the holding open and never sends MKT.

This is a **timing-dependent interruption**, not a guaranteed cancel rejection.
If all pairs finish before disconnect, mark the intended failure NOT OBSERVED.
Use I01–I03 below for a deterministic cancellation failure.

### F04 — One leg cancelled/filled during bracket creation

1. Fresh C6; prepare six layers. During creation, watch for the first accepted
   stop. Manually cancel it in TWS before its target is accepted if timing permits.
2. If detected before target send, no corresponding target may follow. If the
   race occurs after the last check, a target can already be in flight; require
   detection, halted later tranches, unknown status and a surviving-leg warning.
3. Repeat in separate fixtures with a target close enough to fill naturally
   during creation, and a stop close below bid that may trigger during creation.
4. Require no further send **after the conflict is detected**, no automatic
   orphan cleanup, and accurate remaining exposure after Refresh/restart.

The pre-send check and TWS send are not atomic. Do not write a pass criterion
that promises an orphan can never exist. Record timestamps/IDs; missed windows
are NOT OBSERVED. Controlled callback sequencing belongs in I04.

### F05 — TWS precaution hold or rejection

1. Fresh C1: retain current TWS precautions; choose a paper test price/quantity
   that its existing precaution settings flag, if a suitable enabled limit is
   available. Record which condition actually fired; do not disable a gate.
2. If a Transmit hold appears, leave it pending beyond the app deadline.
   Expect pending/unknown, no duplicate on Refresh/restart, no manageable
   complete pair unless exact accepted evidence exists.
3. Inspect **both** legs in TWS, including untransmitted rows. Either transmit
   only the reviewed correct pair or cancel both manually. Refresh/Verify must
   use broker evidence; simply ticking a confirmation cannot release the lock.
4. If rejected, verify any surviving sibling remains visible and blocked.
   A system rejection must not be assumed to cancel its sibling automatically.

No enabled precaution means this fixture is BLOCKED, not proof that held or
rejected orders work. Precaution holds and broker rejections are different;
record which occurred and use injection for the other branch.

### F06 — Process termination with possible broker writes

Use the operating system's Force Quit on **this test app**, not TWS, in each
separate fresh fixture:

1. C4: quit while new brackets are being sent/acknowledged.
2. C4: quit after at least one pair cancellation during conversion.
3. C7: quit just after pressing Confirm on its MKT exit.
4. Working trail: quit during amendment or deletion.

Before relaunch, inspect TWS and capture orders/fills. Relaunch with the same
paper journal/client ID. Require no automatic resend, persistent uncertainty
where evidence is incomplete, and reconciliation only to exact owned IDs.
If quit landed before any write, record that phase; it does not prove the
post-send crash boundary. Resolve the account before the next fixture.

### F07 — Recovery evidence must be real

1. Continue a held/unknown bracket from F05. Manually cancel both exact legs
   in TWS. Open Verify/Verify bracket no longer exists when offered.
2. Check the selected layer identity, confirm only what you saw in TWS, and
   request verification. A fresh complete broker read must prove no surviving
   working leg or attributable SELL execution before clearing cancellation.
3. Repeat with one sibling intentionally left working: clearance must fail.
4. After verified absence, recreate the same draft. Require fresh OCA identities,
   no old-order adoption and only one new attempt. Hide a confirmed-cancelled
   row; verify it sends nothing and keeps durable history.

### F08 — Closed position, surviving order and restart

Paper-only adverse fixture: leave a one-sided app-owned pair, then close the
position manually in TWS. Inspect/cancel the surviving sell promptly to prevent
an accidental paper short; capture the warning first if feasible.
Check the app while closed, then restart and check again. No order management
may be enabled against a nonexistent holding. An unresolved surviving order
must remain discoverable through the app's recovery/status surfaces.

An earlier audit documented a restart/no-position recovery visibility gap in
[BRACKET_VALIDATION.md](BRACKET_VALIDATION.md). Do not assume it is fixed from
that document or silently waive this case: verify this build. If the warning
disappears while a sell survives in TWS, record FAIL even if duplicate submission
is internally blocked. This scenario is especially suited to injection when
safe manual timing is not achievable.

### F09 — Partial fills and OCA reductions

1. Fresh C5 with a quantity-2 first layer. Put its target close above ask and
   observe a natural fill; use TWS time-and-sales/executions to identify whether
   it really filled 1 of 2 while another 1 remained.
2. If partial, record held quantity, both legs' original/filled/remaining
   quantities and OCA sibling reduction. Require coherent reservation of the
   remaining exposure and no editable fully unfilled layer assumption.
3. Try management of the partially filled layer: it must fail closed where
   unsupported, rather than resell the original 2. Other complete layers may
   only be managed if the fresh total exposure remains coherent.
4. Repeat opportunistically for a trail and MKT exit. Inspect partial fills,
   pending remainder, any terminal rejection and final history independently.

Do not buy excessive quantities or choose illiquid options merely to force a
partial fill. Paper fills are simulator-controlled; full execution in one
callback is NOT OBSERVED for the partial-fill branch. Deterministic I05 remains
necessary for unsupported partial paths. IBKR notes that some exchange-directed
paper market remainders may be rejected; capture routing before interpreting it.

## 8. Invalid and unusual input through the actual desktop UI

Use C4, initially with **no orders**. For active/trailing editors, use a separate
accepted fixture and keep TWS visible. Run each row by typing, pasting, tabbing
out and pressing Enter; include opening/closing dialogs with keyboard focus.
The extra manual evidence is browser/decimal/form behavior and that the actual
writer stays gated, not another proof of arithmetic already covered offline.

| ID | Inputs/actions | Required result |
| --- | --- | --- |
| V01 | Quantity: blank, 0, −1, 1.5, 5 against holding 4, a very long integer; then valid 1. | Reject or visibly constrain invalid allocation; no silent fractional/truncated/overflow order. Valid correction restores a usable review. |
| V02 | Price/percent: blank, whitespace, letters, `NaN`, `Infinity`, `1e309`, `1,25`, `1,000.00`, `$5`, Unicode minus `−5`, emoji. | Explicit validation or documented visible normalization. No crash, nonfinite amount, silent reinterpretation or broker write. If numeric controls refuse a paste, record that UI boundary and continue. |
| V03 | Valid equivalences `1`, `1.0`, `1.00`; long decimal near a tick boundary; price just below/at/above a market-rule band boundary if available. | Economically equivalent review values; tick-rounded price stays identical through blur, Review and Confirm. Test valid submission only with a safely nonmarketable price. |
| V04 | Stop 0, stop negative, stop equal/above target; valid stop at B/E and above entry but below target. | Invalid price relationships block. Valid stop above entry is not rejected solely because its draft loss percentage is negative. Observe any immediate-sell warning separately. |
| V05 | Draft loss −20%, 0%, +20%; active stop return −20%, 0%, +20%, inspecting labels each time. | Draft loss and active signed-return conventions are clear: a negative draft loss raises trigger above entry, while negative active return puts it below entry. Review premium price must match intended direction. Cancel before any unsafe immediate trigger. |
| V06 | STP LMT offset blank/0/negative, excessive gap, percent ↔ dollar switch; extremely low positive resulting limit. | Invalid offsets block; valid low-price floor obeys verified market rule and remains below trigger. Review never swaps percent and dollars. Do not send an extreme limit solely to prove rounding. |
| V07 | Trail distance blank/0/negative/off-tick dollar amount; percent ≥100; offset making limit ≤0. | Cannot review invalid trail. Empty offset means TRAIL, not missing required trail. Positive valid offset means TRAIL LIMIT. No stale previous valid value used after invalid input. |
| V08 | Common stop dialog: exact premium with many decimals, switch price/percent, save, reopen. | Selected tick price retained despite percentage display rounding; current observed price and proposed price are distinguishable. Confirm review equals the value actually saved. |
| V09 | Settings presets: blank list, repeated separators, text, out-of-range percentages; remove last draft, change default STP↔STP LMT, recreate. | Invalid defaults explained; Build Draft cannot partially save malformed layers. New drafts inherit valid new defaults; existing drafts retain their chosen configuration. Settings alone create no TWS orders. |
| V10 | Review then Cancel, close dialog, reopen, press Escape/Enter, change selection and Refresh with unsaved/invalid fields. | No accidental Confirm or stale armed action; no changes to TWS. User can recover from validation without restarting or losing unrelated valid work unexpectedly. |

## 9. Inventory, observations, sessions and evidence quality

| ID | Manual actions | Required result |
| --- | --- | --- |
| U01 | Start on empty paper portfolio; buy C1 manually. Then buy P2 while C selected. | Verified arrivals appear without hijacking selection; empty/unknown/failed read are distinct states. A healthy subscriber is not itself permission to write. |
| U02 | On selected C with an unsaved draft, manually buy 1 more; later reduce quantity in paper. | Quantity-change notice preserves draft intent; unavailable/overallocated quantities cannot be sent. Fresh review uses new verified position. |
| U03 | Disconnect/reconnect idle TWS and Refresh repeatedly. | Indicator reflects health, no stuck busy spinner, no duplicate orders/subscribers, recovery to a usable workspace. Empty portfolio remains responsive. |
| U04 | Manually cancel an owned trail in TWS without changing holding. Wait for observer, then Refresh. | Cancelled state eventually reconciles without requiring a position change; missing/uncertain order is not falsely sold. Record latency and whether fallback Refresh was needed. |
| U05 | Run a positive bracket/trail case during supported regular hours and repeat in an eligible SPX extended session shown by TWS. | Record local time plus UTC/exchange session; inspect both legs' Outside RTH/2109 warning if any, actual accepted/held state and any trigger. Flag alone is not proof of an extended-hours fill. Unsupported session is BLOCKED. |
| U06 | After full fills, inspect sold rows and session closed-position history; restart later and inspect available persisted history. | Actual weighted fills and TWS reported realised P&L distinguished from estimates. Zero is zero; missing reports remain pending. No invented P&L from disappearance/last quote. Session-only navigation need not persist, but durable fill records must not disappear. |
| U07 | Keep app open across trading-date change; refresh old closed rows and any working GTC orders. | Old execution history is not fabricated if TWS no longer supplies it; already persisted evidence retained. GTC not silently converted to DAY or resent. Missing evidence gates actions. |
| U08 | Narrow/resize window; use keyboard only to select, edit, review, Cancel and Confirm. Test scroll to final layer in C6. | Contract, quantities, warning, confirmation countdown and destructive-action labels remain readable/reachable. Tab/focus/Enter cannot accidentally transmit. Color is not the only status signal. |
| U09 | Where the paper setup supplies delayed/frozen/unavailable quotes, open MKT projection, trail dialog and price editor. | Quote type labelled; unavailable bid never treated as zero/current. Trail fresh-bid requirements block. Orders-only cancellation must not be confused with a missing-market-data failure. No need to alter live subscriptions to force this. |
| U10 | If stocks, shorts, multi-leg spreads, futures options or fractional holdings are already present in the dedicated paper account, inspect inventory and try selection without opening more unsupported positions. | Only verified positive integral single-leg OPT positions become eligible; unsupported holdings cannot acquire closing plans. Missing fixtures are BLOCKED and the detailed rejection variants stay covered offline. |

## 10. Failures TWS alone cannot reliably produce

The current automated suite already injects most failures below with simulated
clients. The remaining question is whether the real desktop/operator recovery
works with a controlled transport fault. **No operator-facing deterministic
fault-injection controls were identified in the current app.** These are
engineering-assisted acceptance specifications, not runnable TWS buttons.
Mark BLOCKED until a disposable paper-only harness exists; do not mark passed
because an offline test passed or a random disconnect happened.

A suitable harness must be explicitly isolated from live transport, retain real
read evidence, target an exact operation/order and fail once at a recorded
boundary. It must not globally drop traffic or fabricate successful acceptance.
This runbook does not add or enable that harness.

| ID | Controlled fault required | Acceptance evidence beyond existing offline tests |
| --- | --- | --- |
| I01 | Fail cancellation of pair 2 after pair 1 has fully cancelled, before any cancel request for pair 2. | Desktop reports exactly one completed pair, leaves later pairs unchanged, never submits replacement and survives restart without resuming. Exercise conversion, cancel-all and whole-position exit separately. |
| I02 | Cancel target of pair 2 but reject/omit cancellation of its stop, or reverse the legs. | One-sided state displayed with precise IDs, no later pair cancel/replacement, unresolved management lock. Manual verification cannot clear while survivor works. |
| I03 | TWS cancels both legs but drop only their acknowledgement to the app. | Unknown rather than false success; later complete exact terminal evidence may resolve. No duplicate sell/cancel on reconnect/restart. This distinguishes “cancel failed” from “result unknown.” |
| I04 | Deliver held/rejected/missing stop, fill between pre-target read and send, delayed/reordered/duplicate callbacks, late warning after rejection. | Actual GUI warnings and chronological trace match injected phase; no later send after detected fault, no false success, visible surviving legs and recovery lock. |
| I05 | Deliver partial target/stop/trail/MKT fills, then rejection/cancel/disconnect before remaining completion. | Desktop shows actual filled and remaining quantities, correct reservations/history; unsupported management blocked. Duplicate execution callbacks do not double-count P&L. |
| I06 | Accept MKT/TRAIL/amendment at TWS; drop acknowledgement, then terminate app. | Restart retains intent and unknown state, no resend; exact fresh evidence required for resolution. |
| I07 | Isolated journal: deny intent write, then separately fail receipt persistence after accepted broker write. | Before-write failure produces no order; after-write failure preserves durable blocking intent/recovery. Operator sees uncertainty after restart. Never chmod/corrupt the normal journal used for active paper orders. |
| I08 | Omit completed-order barrier, execution completion, market rule, contract field, or commission/P&L report independently. | Relevant actions fail closed; P&L pending rather than guessed; unrelated verified history retained. Distinguish missing quote from orders-only reads. |
| I09 | Crash after PREPARED, after each send, after each acknowledgement, and after receipt replacement. | Exact boundary recorded; restart never resends unknown action. Include no-held-position surviving-order visibility from F08. |
| I10 | Fresh reads initially succeed; after final pair cancellation return a changed quantity/cost/fill set or unusable bid. | Conversion/exit stops before replacement, reports cancelled count and whether send attempted. Protection gap is explicit. |

Do not spend manual sessions repeating every permutations table in the offline
suite. Use these injections to test end-to-end operator behavior at each
distinct failure boundary. Natural paper observations remain separately needed
for acceptance, routing, OCA reactions, precautions and real callback timing.

## 11. Coverage map and execution order

The older audit predates several test refactors and journal fixes. The mapping
below uses the current files, not its historical test counts. This is a
source-based assessment, not a measured line/branch coverage claim.

| Current automated coverage | Manual complement |
| --- | --- |
| `test_planner.py`, `test_outcome.py`, `test_portfolio.py`, `test_snapshot.py` | J09/J12, O01–O09, V01–V09: actual UI entry, TWS reservations, correct contract, market-rule echoes |
| `test_bracket_transmission.py`, `test_execution.py` | J01/J06/J08/J10, F04/F05: actual TWS stop-first acceptance, precautions and OCA fill behavior |
| `test_app_orders.py`, `test_market_orders.py` | J02–J05, F03, U06: full-position versus layer quantities, actual cancellation and MKT lifecycle |
| `test_app_trailing.py`, `test_trailing_outcome.py`, `test_execution.py` | J01/J07, T01–T11: broker trailing units, ratchet, type switches and fills |
| `test_execution_journal.py` | F06/F07, R12, I07/I09: real process exit, visible recovery, desktop multi-instance behavior; file faults already have automated coverage |
| `test_app_workbench.py`, `test_app_browser.py`, observation/connection tests | R01–R11, V01–V10, U01–U09: human interaction, live TWS events, focus, session and UI evidence |

Suggested passes:

1. **First session / smoke:** J01–J05 (J02 currently blocked), J09, J12/J13,
   F01, R01/R07, cleanup.
2. **Execution semantics:** J06–J11, T01–T11, O01–O08. Natural fills/ratchets may
   span more than one session; report their state honestly.
3. **Adverse state:** R02–R12, F02–F09. Use a fresh fixture after every conflict.
4. **Desktop and session:** V01–V10, U01–U10. Group no-write inputs before
   buying another fixture; use demo mode for rehearsal only.
5. **Engineering acceptance:** I01–I10 wherever natural timing left gaps.

For a release, run the full applicable suite against one identified build.
Report missing sessions, fixtures and injections as open coverage, not blanket
“fully tested.” Any changed execution implementation requires rerunning its
affected journeys and failure boundaries.

Offline baseline commands, run independently of paper tests:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

## 12. Cleanup protocol

1. Inspect all working orders for the exact test account/conId in TWS, including
   held/untransmitted orders and every OCA sibling. Capture uncertain evidence.
2. Cancel remaining **test** exits deliberately in TWS; wait for terminal
   statuses and inspect again. Do not use a global account cancel.
3. Check actual held quantity and recent executions. Only after no conflicting
   closing orders remain, manually close the **current remaining** paper long
   quantity, or use a newly verified app exit if the state is healthy/actionable.
4. Wait for fills; verify position flat and no working/held test orders. A
   cancellation request or disappearing portfolio row alone is insufficient.
5. Refresh the app and complete supported recovery verification. Preserve
   uncertain state if evidence is incomplete; do not delete/edit the journal
   to unblock another attempt.
6. Restore settings changed by the case. Stop the test instance before copying
   its journal for evidence. Keep its sidecar lock file intact.

On macOS the paper journal is normally
`~/Library/Application Support/IBKR Options Manager/paper-execution-journal.json`.
Cancellation/conversion/exit timing traces are normally under the adjacent
`logs/bracket-cancellations.jsonl`; price-update traces are described in
[PAPER_EXECUTION.md](PAPER_EXECUTION.md). Protect evidence containing account,
contract and order identifiers; redact shared copies.

## 13. Copyable run record

Copy this block per case/variant; use one result for each independently tested
action family, input group or fault boundary.

```text
Run ID / case ID / variant:
Result: NOT RUN | PASS | FAIL | BLOCKED | NOT OBSERVED
Tester / timestamp (Europe/London and UTC):
Git commit / local changes:
TWS version / API version / OS / app launch mode:
Paper account alias / client IDs / port:
Quote type / session / routing / precaution configuration:
Contract: conId / local symbol / expiry / strike / right / class / multiplier / currency:
Starting held quantity / entry basis / working reservations:
Draft quantities / reviewed rounded prices / trail units and offsets:
Exact action sequence / interference or fault phase:
TWS IDs + permanent IDs + OCA groups before and after:
Executions: IDs / side / quantity / time / price / realised P&L availability:
Expected result:
Observed result / precise failure reason:
Confirmed cancellations / surviving orders / replacement attempted?:
Refresh result / restart result / duplicate-order check:
Screenshot / trace / journal evidence paths:
Cleanup: current position flat? all test orders terminal? uncertainty resolved?:
Issue reference / next action / remaining unobserved branch:
```

Release acceptance requires all applicable cases passed, all uncovered branches
explicitly listed, no unresolved unsafe order state, and matching broker evidence
for every claimed fill/cancellation. Paper success remains evidence for this
paper environment only; live transmission is a separately authorized milestone.
