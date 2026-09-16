# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive

## Stack

Python 3.12 with PySide6 for the local desktop interface, backed by the
existing transport-neutral snapshot coordinator and pure planner. The official
IBKR Python API remains isolated behind the read-only broker seam.

## Users

The primary user is an individual options trader working in paper TWS who has
already opened a long single-leg option position and needs to inspect a
protective stop and profit-taking ladder before any future submission workflow
exists.

## Product Purpose

Turn one fully verified paper-TWS option position into a transparent,
deterministic preview of closing target/stop OCA pairs. Success means the user
can see the exact observed state, allocation arithmetic, prices, grouping, and
every blocking validation without the application being able to transmit an
order.

## Positioning

The product combines exact IBKR contract identity and coherent broker snapshots
with a pure order-plan engine. It does not hide safety gates behind a generic
trading dashboard or pretend that previewing and transmitting are the same
operation.

## Operating Context

The application runs locally beside a paper Trader Workstation session using a
loopback API connection, nonzero client ID, exact paper-account allowlist, and
read-only API setting. The user refreshes broker state, selects an eligible long
option, configures tranche and stop parameters, and inspects the resulting plan.

## Capabilities and Constraints

- Milestone 1 is permanently read-only and has no order placement,
  modification, cancellation, binding, exercise, or global-cancel path.
- Only positive, integral, fully verified single-leg `OPT` positions are
  eligible.
- Account, conId, security type, expiry, strike, right, multiplier, currency,
  trading class, and local symbol must agree across observed state.
- Incomplete, stale, ambiguous, disconnected, conflicting, or malformed state
  fails closed.
- The preview exposes basis normalization, visible allocation, market-rule
  rounding, trigger method, TIF, OCA grouping, and all validations.
- Credentials are never persisted and descriptive account data is redacted in
  ordinary diagnostic output.

## Evidence on Hand

- Product and safety requirements: `docs/PROJECT_BRIEF.md` and
  `docs/MILESTONE_1_PLAN.md`.
- Passing redacted read-only paper-TWS evidence under `tests/fixtures/`.
- A complete pure planner and coherent snapshot adapter with an offline safety
  suite.
- No logo, commercial claims, or external brand assets exist and none should be
  fabricated.

## Product Principles

- Make observed state and derivation inspectable.
- Fail closed whenever broker truth is incomplete or ambiguous.
- Keep previewing structurally separate from transmission.
- Prefer one linear workflow over configurable dashboard chrome.
- Preserve exact contract and account identity at every seam.

## Accessibility & Inclusion

Keyboard-complete operation, visible focus, sufficient contrast, semantic field
labels, and status communication that does not rely on color alone are required
for the desktop workflow.
