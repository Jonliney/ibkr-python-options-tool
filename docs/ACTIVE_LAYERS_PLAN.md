# Active layers plan

## Purpose

Keep the existing **Draft layers** workflow focused on composing a new,
read-only OCA bracket plan. Add a separate **Active layers** tab for brackets
that this application created and can therefore identify, reconcile, and
eventually manage without touching external TWS orders.

This plan does not authorize live transmission. The explicit paper-only
milestone permits one narrow active-layer action: cancelling one exact
app-owned OCA pair and then submitting a standalone MKT after two
fresh-snapshot checks and a second confirmation. All other active-layer actions
still produce inspectable plans only.

## Tabs

### Draft layers

- Retains the current editable layer rows, equal split modes, outcome
  projection, and preview-only action review.
- Shows only contracts verified as available for new brackets.
- Remains blocked when external orders cover the position.

### Active layers

- Shows only complete OCA pairs created by this application for the selected
  account and option contract.
- Each row shows: layer name, OCA group, quantity, limit target, stop price,
  time in force, remaining quantity, pair status, and last reconciliation
  time.
- A clear empty state distinguishes “no app-managed layers” from unavailable
  or stale broker data.
- External orders remain visible as inspection-only information, never as
  active-layer rows or action targets.

## Required ownership and reconciliation model

Persist an application-owned record when a pair is created. It must include:

- account, conId, and the fully verified option identity;
- application-generated logical layer ID and OCA group;
- IBKR order IDs and permanent IDs for both LMT and STP orders;
- submitted quantity, prices, TIF, creation time, and the snapshot/connection
  epoch that produced the intent;
- lifecycle state, fills, rejection/acknowledgement evidence, and a durable
  audit trail.

On every refresh and before any future action, reconcile these records against
open orders, executions, and position quantity. An active layer is actionable
only when both legs, their ownership markers, account, contract identity,
remaining quantity, and OCA relationship match exactly. Any missing,
partially-filled, manually changed, stale, or ambiguous state fails closed and
explains why it needs inspection in TWS.

## Future action flows

All flows first build a chronological plan, then require an explicit final
confirmation. They never modify orders not proven to be application-owned.

| Action | Intended plan | Required final checks |
| --- | --- | --- |
| Sell now (one layer) | **Paper-only:** cancel the owned LMT and STP pair, confirm both cancellations plus a client-scoped open-order recheck, then submit a new standalone MKT for its verified remaining quantity. | Two fresh account/contract/order/position snapshots, pair ownership, exact client ownership, unchanged remaining quantity, complete two-leg OCA relationship, cancellations, recheck, and MKT acknowledgement. A timeout is indeterminate: inspect TWS, never retry blindly. |
| Close all brackets | Select all eligible app-owned layers, cancel every leg, verify acknowledgements, then submit exits for the reconciled remaining position quantity. | Same checks per layer plus a final position-level quantity reconciliation after all cancels. |
| Update STP | Change only the owned stop leg(s) to the requested tick-valid price while preserving quantity, OCA link, parent relationship, and TIF. | Fresh pair details, no fill/change since review, verified tick rule, broker acknowledgement, post-change re-read. |
| Move to B/E | Resolve the verified average entry cost, round it to the current contract tick rule, and prepare an Update STP plan for the selected layers. | Explicitly show the source entry basis, rounded stop value, eligible remaining quantity, and each affected order before confirmation. |

The exact exit order type for **Sell now** and **Close all** remains a product
decision before implementation. “ASAP” could mean a market order, a protected
market variant, or an aggressively priced limit order; the choice changes
execution risk materially and must be made explicitly rather than inferred.

## Delivery sequence

1. Define the durable app-owned OCA-layer record and reconciliation domain
   model, with fixtures for restarts, disconnects, manual TWS changes, partial
   fills, rejections, and duplicate broker callbacks.
2. Add the Active layers tab and its status/empty/conflict states. The narrow
   paper-only Sell now action is permitted only behind its explicit feature
   gate; no other action transmits or modifies anything.
3. Add pure action-plan builders for Sell now, Close all, Update STP, and Move
   to B/E. Render the complete chronological plan in the action review panel.
4. Add a paper-only execution adapter behind an explicit feature gate, with
   acknowledgement, re-read, idempotency, and audit-log requirements.
5. Test every flow in paper trading, including adverse races and partial
   failures. A separate explicit decision is required before any live-account
   transmission work.

## Open decisions for the next design session

- What protected exit order type should “Close all” use? Sell now is a
  paper-only cancel-then-MKT experiment; it needs paper validation before any
  broader execution decision.
- Should Close all operate only on selected app-managed layers, or include
  every app-managed layer for the contract by default?
- Does Move to B/E use the current verified average cost basis for the entire
  position, or the historical basis captured when each layer was created?
- Should Update STP permit a shared price across selected layers, or expose a
  per-layer editor plus a bulk apply control?
