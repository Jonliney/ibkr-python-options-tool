# Project brief

## Problem

The trader buys a long call or put in TWS and sells it before expiration. A
typical position contains 4, 5, 6, 10, or another whole number of contracts.
They need to:

- protect the whole position with a downside stop;
- take profits in tranches, commonly two contracts at +20%, two at +40%, and
  continuing upward;
- handle any remainder explicitly;
- avoid splitting the entry into many small orders because per-order fees are
  material for inexpensive contracts;
- remove the repetitive and error-prone construction of OCA exit orders in
  TWS.

Native TWS hotkeys do not provide the required options workflow in the user's
environment. A ScaleTrader plus OCA construction is not sufficiently confirmed
for production use. The project therefore focuses on a constrained local TWS
API companion.

## Recommended order model

For an already-filled long option position, create equal-quantity independent
OCA pairs:

- target tranche: SELL LMT at the configured profit level;
- protective tranche: SELL STP at the configured loss level;
- both orders have the same quantity and a unique OCA group configured to
  reduce with overfill protection.

Example for ten contracts with tranche size two:

1. Two at +20% paired with a stop for two.
2. Two at +40% paired with a stop for two.
3. Two at +60% paired with a stop for two.
4. Two at +80% paired with a stop for two.
5. Two at +100% paired with a stop for two.

The remainder policy for quantities not divisible by the tranche size must be
chosen and displayed explicitly. Percentages should normally use the actual
position cost/fill basis rather than a transient bid or ask.

## Milestone 1: read-only planner

- Connect only to a paper TWS session with the API in read-only mode.
- Read managed accounts, positions, exact contract details, working orders,
  quotes, market rules, and connection state.
- Allow selection only from existing long single-leg option positions.
- Generate the complete proposed order graph without calling `placeOrder`.
- Display account, paper/live state, contract, current position, already
  allocated quantity, price basis, rounded prices, TIF, target tranches, stop
  orders, OCA groups, and every validation result.
- Persist no credentials.

## Later milestones

1. Paper-only submission with explicit arming and confirmation.
2. Reconciliation, restart recovery, idempotency, and audit logging.
3. Adversarial testing of partial fills, rejected legs, disconnects, stale
   quotes, duplicate clicks, TWS-side edits, and application crashes.
4. A minimum-size controlled live trial, only after separate authorization.

## Core invariants

- Only existing positive `OPT` positions may be managed.
- Generated actions are closing `SELL` orders only.
- Total allocated tranche quantity may not exceed the unallocated position.
- Every target tranche has equal stop coverage in its own OCA group.
- A plan is idempotent: repeated confirmation cannot create duplicate exits.
- Existing unrelated orders are never modified or canceled.
- Invalid or uncertain state prevents submission.
- TWS API order precautions remain enabled and connections are localhost-only.

## Execution risks to keep visible

- A stop order can execute materially below its trigger price.
- A stop-limit order can remain unfilled.
- Option stops may be affected by wide or transient bid/ask spreads and the
  configured trigger method.
- Paper trading simulates stops and complex orders and may differ from live
  execution.
- There is no atomic transaction spanning an arbitrary number of independent
  OCA groups; partial submission failure needs an explicit recovery design.
