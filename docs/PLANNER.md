# Pure exit-plan engine

Slice 1 implements a deterministic, side-effect-free planner behind one public
interface:

```python
build_exit_plan(snapshot: BrokerSnapshot, request: PlanRequest) -> PlanResult
```

The domain package imports neither `ibapi` nor PySide6. It creates immutable
`OrderIntent` values for preview only; it does not create IBKR orders and has no
transport, file, clock, environment, or GUI dependency.

## Plan model

A valid result contains independent target/stop pairs. Both intents in a pair:

- are closing `SELL` actions for the exact selected account and `conId`;
- have the same positive integral quantity;
- share a deterministic logical OCA group;
- display intended OCA type `2` (proportional reduction with block);
- retain both raw and market-rule-rounded prices.

Targets are sell limits and stops are sell stop triggers. Both prices round
upward to avoid silently selecting a lower target or protective trigger. The
rounder reselects the price band when rounding crosses a market-rule edge.

## Allocation and remainders

Existing ungrouped closing sells consume their full remaining quantity. A
recognized existing OCA target/stop pair consumes its maximum remaining leg,
not the sum of both mutually exclusive legs. Mixed contracts, unknown shapes,
unequal quantities, invalid statuses, duplicate permanent IDs, and fractional
or nonpositive remaining quantities block planning.

Two remainder policies are supported:

- `NEXT_RUNG` creates a smaller pair at the next target percentage;
- `ADD_TO_LAST` enlarges the last full pair and is invalid when no full tranche
  exists.

No policy may leave a remainder unprotected.

## Fail-closed inputs

The planner returns `BLOCKED`, no fingerprint, and no pairs when any required
state is unknown or invalid. Checks cover connection/read-only/account evidence,
snapshot freshness and completion, request errors, connection epoch, exact
option identity, positive integral position, multiplier-normalized cost basis,
quote quality and type, market-rule structure and exchange, existing closing
exposure, target sequence, stop percentage, TIF, trigger method, and available
quantity.

The fingerprint is a canonical SHA-256 digest over the semantic plan inputs.
Equivalent decimal encodings and callback order produce the same fingerprint;
contract identity, selected-order changes, allocation, or request changes
produce a different one. Unrelated orders do not affect it.

## Verification

Run all checks from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

The suite includes worked examples and Hypothesis properties for quantity
conservation, equal target/stop coverage, closing-only intents, stable logical
groups, deterministic fingerprints, and directional tick rounding. No default
test requires TWS or network access.

This planner is preview-only. A valid result is not authorization to submit,
modify, cancel, or exercise anything, and paper evidence does not establish
live execution behavior.
