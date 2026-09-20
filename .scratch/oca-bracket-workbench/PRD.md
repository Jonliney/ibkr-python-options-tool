# OCA Bracket Workbench PRD

Status: needs-triage

## Problem Statement

A trader holding individual long calls and puts needs to understand every open
option position, see which contracts can still receive exit protection, and
construct/manage layered OCA exit brackets without manually rebuilding the
same order graph in TWS.

The current workflow is cognitively expensive. A trader must reconcile current
holdings with working orders, split quantities, calculate tick-valid target and
stop prices, remember which orders they created, and infer whether a proposed
change is safe. A simple inventory must not imply that a working sell order has
already filled. Equally, the app must not expose unreserved contracts as
available when they are already covered by another closing order.

The product must first provide a complete, read-only design and deterministic
preview workflow. It must then be able to evolve into a tightly constrained
order-management workflow for brackets created by this application, with clear
broker reconciliation and explicit confirmations.

## Solution

Create a desktop-first OCA Bracket Workbench for individual long option
positions. The product presents a compact inventory of all active option
positions, a selected-position workspace, a cost-basis price explorer, a
layered OCA draft editor, a management list for application-created brackets,
and a chronological action-review surface.

The design milestone is read-only: it can inspect, calculate, allocate, and
preview, but cannot place, modify, or cancel orders. The design must nevertheless
include the future create, edit, delete, bulk-stop, confirmation, success, and
reconciliation workflows so their safety properties can be evaluated before
write capability is implemented.

The product is account-configurable. The selected account identity and
connection state are always prominent. The current implementation remains
read-only; any future write capability, including use with a live account,
requires a separately authorised implementation milestone and paper validation.

## User Stories

1. As a trader, I want to see every active option position in one inventory so
   that I can choose the position I need to manage without searching TWS.
2. As a trader, I want each inventory item to be a compact tabular button so
   that the inventory is dense, scannable, and easy to select.
3. As a trader, I want each position button to show contract, `Open / Total
   qty`, cost basis, and a compact state so that I can orient myself before
   opening a workspace.
4. As a trader, I want `Open / Total qty` to remain a simple held-position
   field, such as `10 / 10`, so that working orders are not misrepresented as
   executed sales.
5. As a trader, I want unsupported or unsafe positions to remain visible but
   inspect-only so that the inventory is complete without offering an unsafe
   action.
6. As a trader, I want the selected position to remain visibly selected so
   that I always know which contract a draft or management action concerns.
7. As a trader, I want to see the active account, connection status, last
   verified refresh state, refresh control, and settings entry point at all
   times so that I can assess operational context immediately.
8. As a trader, I want the creation area to tell me the verified number of
   contracts available to bracket so that I cannot accidentally cover contracts
   already associated with active closing orders.
9. As a trader, I want active closing orders created outside this app to reduce
   availability without becoming editable in this app so that the tool respects
   external order ownership.
10. As a trader, I want a selected position to show its existing
    application-created brackets continuously so that their targets, stops,
    quantities, and states inform my next action.
11. As a trader, I want a new draft to start with one OCA layer so that a
    simple one-target exit is fast to set up.
12. As a trader, I want the initial layer to use configurable default target,
    stop, and time-in-force values so that my common rules do not require
    repetitive entry.
13. As a trader, I want to choose all available contracts or a specified
    quantity up to the verified available quantity so that I can either cover a
    full remainder or make a deliberate partial exit plan.
14. As a trader, I want an Add layer action to evenly redistribute the
    unsubmitted draft quantity so that I can quickly create a staged exit.
15. As a trader, I want each newly added layer to receive the next configurable
    target percentage so that a standard ladder starts from sensible defaults.
16. As a trader, I want to directly edit every draft layer's quantity, target,
    stop, and time in force so that unusual allocation and price plans remain
    possible.
17. As a trader, I want an odd contract remainder to be visible in the layer
    allocation so that no quantity is silently dropped or duplicated.
18. As a trader, I want to add a Runner layer with a configurable high default
    target, such as +150%, and its own stop so that a residual position remains
    protected while allowing an extended move.
19. As a trader, I want every ordinary layer and Runner to be an independent
    equal-quantity SELL LMT and SELL STP OCA pair so that either exit path
    protects the same contracts.
20. As a trader, I want partial fills to reduce the counterpart leg to the
    remaining quantity so that a layer remains protected without creating an
    oversell risk.
21. As a trader, I want a cost-basis price explorer so that I can translate
    percentage rules into actual option prices before editing the order draft.
22. As a trader, I want the explorer to calculate TP as cost basis plus a
    percentage and SL as cost basis minus a percentage so that the percentages
    represent my own trade economics rather than a transient quote.
23. As a trader, I want independent TP and SL sliders, both with quick-choice
    controls, so that I can rapidly compare common levels.
24. As a trader, I want the explorer to default to +20% TP and -20% SL and
    allow TP up to +200% and SL down to -100% so that it supports both routine
    exits and wider exploration.
25. As a trader, I want the explorer to show the current Ask separately so
    that I can compare my basis-based rules with current market context.
26. As a trader, I want the explorer to show valid tick-rounded prices, gross
    P&L per contract, and gross P&L for the selected quantity so that I can see
    the economic effect of each proposed level.
27. As a trader, I want the explorer to state that gross P&L excludes
    commissions, fees, slippage, spread, and partial-fill effects so that it
    does not imply a guaranteed net result.
28. As a trader, I want explicit Use as LMT and Use as STP actions so that I
    can copy a calculated value into a draft without creating or modifying an
    order.
29. As a trader, I want direct price inputs to reject invalid increments and
    show the effective rounded price so that the broker will not reject a plan
    for an avoidable tick error.
30. As a trader, I want the price explorer to become unavailable when cost
    basis or tick data is not verified while keeping explicit price entry
    available where otherwise safe so that a calculation aid does not block a
    deliberate absolute-price draft.
31. As a trader, I want to manage only brackets the application can prove it
    created so that it never alters a TWS or third-party order by mistake.
32. As a trader, I want an active, unfilled app-created bracket to allow edits
    to its LMT, STP, and TIF so that I can adapt exits without rebuilding a
    layer.
33. As a trader, I want bracket quantity to be immutable after creation so
    that changing exposure is not hidden inside an ambiguous order edit.
34. As a trader, I want filled, partially filled, external, manually changed,
    stale, or ambiguous brackets to be inspect-only so that I can resolve them
    in TWS instead of the app guessing at their state.
35. As a trader, I want to delete an entirely unfilled app-created bracket by
    cancelling both legs, after reviewing and confirming the exact action plan.
36. As a trader, I want a bulk stop action to apply one explicit new STP price
    to every eligible app-created bracket for the selected contract, defaulting
    to cost basis, while leaving LMT targets unchanged.
37. As a trader, I want every create, edit, delete, and bulk-stop action to
    refresh and compare broker state immediately before execution so that a
    stale review cannot act on changed positions or orders.
38. As a trader, I want to review a chronological, human-readable action plan
    with every order identity, quantity, price, TIF, cancellation, and
    modification before I confirm it.
39. As a trader, I want the future confirmation flow to make non-atomic
    multi-order actions explicit so that I understand that target and stop
    changes are separately acknowledged by the broker.
40. As a trader, I want an incomplete create/edit/delete sequence to stop and
    show an Unknown / reconcile in TWS state so that the product never reports
    success without broker evidence.
41. As a trader, I want defaults in settings to affect only future drafts so
    that changing a preference never silently rewrites an existing draft or
    working bracket.
42. As a trader, I want the desktop interface to remain usable while resized so
    that the workbench is practical on different desktop window sizes.

## Implementation Decisions

### Product and account boundary

- This PRD designs the complete future lifecycle but does not authorise a write
  implementation. The present product remains read-only.
- The account is selected through settings and is not conceptually limited to
  a paper account. Any eventual write milestone must separately authorise
  transmission, validate paper behaviour, and preserve all TWS precautions.
- The supported trading scope is a positive, single-leg, exactly resolved call
  or put. Short options, stocks, futures, combinations, spreads, and ambiguous
  instruments are visible if discovered but inspect-only.

### Workspace and visual direction

- Use a dark, desktop-first workbench with Shadcn-compatible styling and
  components so the visual language can carry forward to web and mobile work.
- Design for resize rather than a fixed canvas. Maintain hierarchy and preserve
  the selected contract, primary action state, and safety feedback at narrower
  desktop widths.
- The design brief must allow multiple layout and navigation explorations. It
  must not force the present implementation's composition.
- The persistent shell contains connection/account state, refresh, and
  settings. Inventory then selected-position content is the primary workflow.
- Position items are tabular buttons. They show contract, `Open / Total qty`,
  cost basis, and a compact status. The simple `Open / Total` field reports
  current held quantity only; it must not infer sold or reserved quantities.
- The selected-position creation area, not the inventory item, communicates
  the actionable available quantity, for example `5 contracts available to
  bracket`.

### Availability and ownership

- Availability is deterministic and derived from a coherent broker snapshot.
  It is current held quantity less coherent, active closing-order coverage.
- A recognizable same-contract OCA target/stop pair consumes the maximum of
  its remaining legs, not their sum. An unpaired closing sell consumes its
  remaining quantity. Ambiguous relationships, unsupported statuses, invalid
  quantities, or coverage exceeding the position block creation.
- External orders are always inspect-only. They reduce availability but cannot
  be changed, cancelled, or claimed by this application.
- An application-order registry persists the minimum durable ownership and
  reconciliation data needed to link an app-created bracket to account,
  contract, broker order identities, logical OCA group, intended parameters,
  and lifecycle evidence across restart/reconnect.
- An order is manageable only when the registry and current verified broker
  state prove ownership and consistency. Missing, manually altered, stale,
  partially filled, filled, or ambiguous brackets are inspect-only.

### Bracket drafting and order semantics

- A layer contains one SELL LMT target and one SELL STP stop with equal
  quantity, unique logical OCA grouping, identical TIF, and proportional
  reduction with broker overfill protection. Each layer is independent.
- A draft starts with one layer using a chosen quantity, default target, stop,
  and TIF. The quantity may be all available or a specific positive quantity
  no larger than available.
- Adding a layer redistributes unsubmitted draft quantity as evenly as possible
  across target layers and assigns the next configured target default. Every
  row remains directly editable before review.
- A Runner is a normal OCA layer labelled Runner. It uses a high configurable
  target default, initially +150%, and a normal stop; it is not a standalone
  protective-stop concept.
- Users may edit target, stop, quantity, and TIF only in an unsubmitted draft.
  After creation, only target, stop, and TIF are editable; quantity changes are
  represented as a future explicit cancel-and-recreate workflow, not an edit.
- Defaults are locally persisted preferences for future drafts only. They
  include initial TP, initial SL, target-step increment, Runner target, default
  TIF, and any default Runner allocation choice.

### Price explorer

- The explorer is a pure calculation tool based on verified positive unit cost
  basis and the contract's verified price-increment rule.
- `TP = cost basis × (1 + TP percentage)` and `SL = cost basis × (1 - SL
  percentage)`. The displayed executable prices are valid tick-rounded values.
- TP and SL have independent sliders, defaulting to +20% and -20%; quick
  choices include 10%, 20%, 30%, 40%, and 50%. TP supports +0% to +200%; SL
  supports -0% to -100%, while preventing zero or negative executable prices.
- The explorer shows current Ask only as market context. It does not calculate
  percentage targets from Ask.
- Gross P&L is calculated from rounded price minus cost basis, verified option
  multiplier, and selected quantity. It is labelled as excluding commissions,
  fees, spread/slippage, and partial-fill effects.
- Use as LMT and Use as STP explicitly copy a displayed rounded price into the
  active draft. They have no broker side effect.
- If cost basis or price-increment data is unavailable, the explorer is
  unavailable. Valid direct absolute price input remains available if the
  broader draft is otherwise eligible.

### Future order-management workflow

- The design includes read-only preview and future execution states. In the
  current milestone, all transmission controls are unavailable and explicitly
  labelled as such.
- Before any future create, edit, delete, or bulk-stop execution, refresh a
  complete broker snapshot and compare it to the reviewed state. Differences in
  account, contract, position, working orders, price increment, connection, or
  freshness invalidate the review and require a new one.
- The review is chronological and exhaustive. It lists each create, modify, or
  cancel with broker identity, order type, quantity, price, TIF, OCA
  relationship, and expected acknowledgement.
- Create, edit, delete, and bulk-stop actions require explicit confirmation.
  Delete means cancelling both legs of an entirely unfilled app-created pair.
- An edit is non-atomic: the future execution plan must show the order of stop
  and target modifications and await confirmation after each. A delete is also
  non-atomic: it waits for both terminal cancellation acknowledgements.
- Any missing acknowledgement, state transition during execution, rejection,
  disconnect, timeout, duplicate-submission risk, or mismatch stops further
  action and surfaces Unknown / reconcile in TWS. The UI must not claim an
  all-or-nothing result without evidence.
- Bulk stop applies one explicit absolute stop price to every eligible active
  app-created bracket for the selected contract. It defaults to cost basis and
  leaves targets unchanged. The review lists every affected stop independently.

### Modules

- **Bracket lifecycle engine:** a pure, deterministic boundary for allocation,
  OCA draft construction, Runner allocation, ownership eligibility, tick and
  P&L calculation, reconciliation classification, and chronological action
  plans. It has no GUI, clock, persistence, or transport dependency.
- **Application-order registry:** durable storage and lookup of app ownership,
  broker identities, intended bracket specification, and lifecycle evidence.
- **Broker gateway:** a capability-separated transport boundary. It remains
  read-only in the initial milestone; a later explicit write boundary handles
  create, modify, cancel, acknowledgement, and reconciliation.
- **Workspace view model:** presentation orchestration that maps verified
  snapshots and pure lifecycle results to position buttons, draft state,
  explorer state, review state, and management state.
- **Desktop workspace:** the Shadcn-styled desktop UI for all inventory,
  drafting, management, review, confirmation, stale, and reconciliation views.

## Testing Decisions

- Tests verify externally observable safety and user outcomes, not private
  implementation structure.
- The bracket lifecycle engine receives exhaustive deterministic coverage for
  whole-contract allocation, odd quantities, layer addition, Runner behaviour,
  requested quantity limits, OCA pair invariants, tick rounding, price-explorer
  P&L, invalid basis/data, and direct-price validation.
- Availability tests cover unpaired sells, valid external OCA pairs, app-owned
  pairs, partial fills, over-coverage, ambiguous groups, external changes, and
  contract/account mismatches.
- Registry and reconciliation tests cover restart, reconnect, missing records,
  changed TWS orders, filled/partially-filled legs, duplicate evidence, and
  ownership ambiguity.
- The future broker gateway is tested with deterministic callback fixtures for
  create, edit, delete, bulk-stop, acknowledgement ordering, partial success,
  rejection, timeout, disconnect, retry, duplicate submission, and stale-plan
  invalidation. Paper-trading validation remains required before any live use.
- View-model and UI workflow tests cover selection, resize-safe presentation,
  no-quote/no-basis states, calculation-to-draft copying, draft allocation,
  review content, confirmation gates, disabled read-only controls, and every
  partial/unknown reconciliation state.
- Existing project practice—pure planner tests, callback replay fixtures,
  read-only snapshot tests, and desktop UI interaction tests—provides the
  testing model for this work.

## Out of Scope

- Implementing order transmission, modification, cancellation, or any live
  brokerage write in the current milestone.
- Managing external, manually entered, third-party, or unprovable app orders.
- Quantity edits for already-created brackets.
- Short option management, stock/future orders, multi-leg options, spreads,
  butterflies, rolling strategies, exercise, or global cancellation.
- Guaranteed fills, stop execution quality, exact net P&L, commission-aware
  accounting, tax accounting, live market-data subscriptions, or reading an
  unsent TWS Order Entry price.
- Mobile-specific screen designs and a responsive mobile implementation.
- Automatic strategy decisions, automatic stop movement, or unattended order
  actions.

## Further Notes

### External design-generation brief

Generate multiple desktop-first design directions while retaining the dark
Shadcn-compatible language. The designs must be usable in a resized desktop
window and must privilege compact inventory scanning, selected-contract clarity,
and safety feedback over decorative dashboard density.

Each direction must cover these states and workflows:

1. Connected inventory with no selected position.
2. Selected eligible long call or put with no app-created brackets.
3. Selected position with external/inspect-only orders reducing available
   quantity.
4. New one-layer draft using defaults.
5. Multi-layer draft with odd quantity allocation.
6. Draft with a high-target Runner OCA layer.
7. Cost-basis explorer with TP/SL slider and quick-choice interactions,
   current Ask context, gross P&L, and copy-to-form actions.
8. Existing app-created bracket management list showing active and
   inspect-only lifecycle states.
9. Edit bracket workflow and chronological non-atomic review.
10. Delete bracket confirmation and partial-cancel reconciliation outcome.
11. Bulk move-stops-to-breakeven workflow and detailed confirmation review.
12. Settings for account/connection and future-draft defaults.
13. Read-only mode with future transmission visually unavailable.
14. Future confirmed-success and Unknown / reconcile-in-TWS outcomes.
15. Stale, disconnected, ambiguous, invalid-cost-basis, and invalid-tick-data
    safety states.

The designs must never imply that an action is atomic when it is not, that an
external order is editable, that a working sell is already filled, or that
price-explorer P&L is guaranteed net profit.

