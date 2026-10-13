# Trailing stop follow-ups

Status: needs-triage

These are pending follow-ups to the paper-only entire-position trailing stop
and trailing stop-limit workflow.

## Work to do

1. [x] Improve the action review sidebar design for trailing stop limits. Make
   the trigger, limit offset, quantity, and risk easy to scan before confirmation.
2. [x] Redesign the active trailing stop-limit layer row to follow the bracket
   layer layout and visual hierarchy while showing its trailing-specific values.
3. [ ] Allow an app-owned trailing stop-limit order to be modified through a
   reviewed, verified paper-order workflow.
4. [ ] Capture and display realized P&L when a trailing exit fills. Investigate
   why the completed sale still showed `$0`. Use TWS-reported realized P&L for
   the exact trailing SELL executions; do not calculate it from fills or track
   fees separately. A missing report stays pending, while a reported zero is
   displayed as zero. The app briefly waits for P&L callbacks after execution
   history completes. Verify the original `$0` symptom with a paper TWS fill;
   the original session has no saved callback trace.
5. [x] Show the trailing layer row after it closes, within the closed-position
   state, using its completed outcome rather than dropping the row.
6. [x] Add the current bid, ask, and average position price to the **Convert
   entire position** dialog, using the contextual pattern from other dialogs.
7. [x] Improve that dialog's wording and spacing. Distinguish converting active
   brackets from placing a trail for wholly unassigned contracts, and simplify
   the copy accordingly.
8. [ ] Show an estimated gain or loss at the initial stop, using the verified
   position basis, quantity, multiplier, current quote, and proposed trail.
   Label this as an estimate because the trigger and fill prices can differ.
9. [x] Expand demo data and dummy scenarios for trailing stops and trailing
   stop limits, including conversion, fills, manual cancellation, and closed
   positions, so the UI can be iterated on without TWS.
10. [ ] Detect when a trailing order is cancelled manually in TWS and update
    the app's state. Check which order callbacks are available in the intended
    client configuration, and retain fresh snapshot reconciliation because a
    callback alone may not establish the final state.
11. [ ] For a trailing order that disappears after a manual TWS cancellation,
    use the bracket layer's unresolved visual treatment: reduced opacity, a
    centered action label that is not a button, and a right-side icon button
    to verify in TWS and clear the row after confirmation. Replace the generic
    **CHECK TWS** badge in this case.

## Safety and verification notes

- Keep order changes paper-only, limited to app-created orders, and gated by a
  fresh account, contract, position, order, and fill reconciliation.
- Derive realized P&L from verified executions and commissions, not the trail
  estimate or a disappearing position alone.
- For TWS event behavior, use [TWS event callback research](../../docs/research/TWS_EVENT_CALLBACKS.md)
  as background, then verify the actual callback and reconciliation behavior in
  paper TWS before relying on it.


## Manual cancellation investigation (2026-10-13)

Item 10 has been investigated; implementation and paper-TWS verification remain
pending. Findings and official API references are in
[Trailing cancellation callbacks](../../docs/research/TRAILING_CANCELLATION_CALLBACKS.md).

The current dedicated observer subscribes only to positions. Cancelling a
trailing order can leave position quantity unchanged, so it need not trigger a
refresh. Even an exact completed-order cancellation currently leaves a trailing
journal entry `SUBMITTED`: completed-order reconciliation handles bracket
entries, not trailing entries. The existing manual-cancel demo models absence
without completed cancellation evidence and deliberately shows `CHECK TWS`.

Recommended sequence:

1. Add deterministic trailing outcome reconciliation from complete fresh
   snapshots, distinguishing cancellation, partial-fill plus cancellation,
   filled, and disappearance without sufficient evidence. Preserve exact
   journal ownership and execution evidence. Do not label `Inactive` cancelled
   or infer a human cancellation from broker status alone.
2. Add bounded periodic nonbinding order reconciliation (initial target:
   15 seconds plus capture time) while connected, serialized with action
   verification. Treat any visible order callback only as a refresh hint; the
   separate nonzero observer ID is not guaranteed other-client order updates.
3. Publish selected-order changes to the protection display without discarding
   unsaved fields. Disarm stale reviews and show one confirmed protection-loss
   notice. Keep item 11's unresolved treatment when the cause is not proven.
4. Verify callback visibility and terminal history fields in paper TWS,
   including same-day cancellation, partial fill then cancellation, reconnect,
   restart, and unavailable completed history. Do not change Master Client or
   bind manual orders to obtain notifications.


## Item 10 implementation (2026-10-13)

Implemented the same-submitter-client order listener, adapter connection leases,
event-triggered snapshot reconciliation, exact terminal cancellation journal
state, and selected-protection notice. Missed callbacks retain a 15-second
recovery check; snapshots remain the authority. See
[paper execution implementation notes](../../docs/PAPER_EXECUTION.md#event-driven-trailing-cancellation-observation).

Item 10 remains unchecked pending actual paper-TWS cancellation, handoff/reconnect
and terminal-field visibility verification. Item 11's unresolved-row dismissal
interaction remains a separate follow-up.
