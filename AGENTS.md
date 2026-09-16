# Project guidance

This software can affect a live brokerage account. Safety properties are part
of the product contract, not optional polish.

## Development rules

- Begin with read-only TWS connectivity and a pure, deterministic order-plan
  engine. Do not add live transmission unless the user explicitly authorizes a
  later milestone.
- Keep domain logic independent from the GUI and IBKR transport layer.
- Resolve options by IBKR contract ID and verify account, security type,
  expiry, strike, right, multiplier, currency, and trading class.
- Fail closed on ambiguity, stale state, disconnects, conflicts, invalid price
  increments, or incomplete acknowledgements.
- Never bypass TWS order precautions.
- Never modify or cancel an order that the application did not create.
- Require tests for quantities, rounding, duplicate submission, partial fills,
  rejections, reconnects, restarts, and manual changes made in TWS.
- Prefer small, reversible changes. Record material safety assumptions in the
  project documentation.

## Verification

Document the applicable test command when the implementation stack is chosen.
Paper-trading success is necessary but is not evidence that live stop and
complex-order execution will behave identically.

## Agent skills

### Issue tracker

Issues live as local Markdown files under `.scratch/`. See
`docs/agents/issue-tracker.md`.

### Triage labels

Use the default triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.
