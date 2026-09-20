# Read-only desktop preview

Slice 3 provides one PySide6 window for refreshing a coherent paper-TWS
snapshot and rebuilding the pure exit-plan preview. It has no arm, confirm,
submit, modify, cancel, bind, exercise, or global-cancel control or transport
path.

## Launch

Start paper TWS first, with read-only API and localhost-only access still
enabled. From the repository root:

```sh
.venv/bin/ibkr-options-manager-gui \
  --account YOUR_FULL_PAPER_ACCOUNT_ID \
  --con-id YOUR_EXISTING_LONG_OPTION_CON_ID
```

The same entry point can be run without prefilling the selection:

```sh
.venv/bin/python -m ibkr_options_manager.app
```

### Simulated data

For interface work or workflow rehearsal outside market hours, use the fully
local deterministic data set:

```sh
.venv/bin/ibkr-options-manager-gui --demo-data
```

It automatically refreshes four illustrative long option positions. The first
includes a simulated external sell limit for five contracts, so both the
available-quantity and external-coverage states can be exercised. The header
states `SIMULATED DATA · NO TWS`; this mode neither connects to TWS nor sends,
modifies, or cancels an order. Optional `--con-id` values must be one of the
contracts present in the simulated data.

The account must use the project's `DU` paper-account allowlist convention.
The exact ID is kept only in memory for the current process. The evidence view
redacts it, and changing any connection-selection field immediately clears the
visible plan and requires another refresh.

## Workflow

1. Verify the literal loopback endpoint, paper port, nonzero client ID, and
   exact paper account and option conId.
2. Select **Refresh paper snapshot**. The previous snapshot and preview are
   cleared before the background read begins.
3. Inspect connection evidence, the exact verified option position, quote,
   market rule, current allocation, and snapshot age.
4. Adjust tranche size, ordered target percentages, stop loss, remainder
   policy, TIF, or explicit stop-trigger method.
5. Select **Rebuild preview**. This reruns only the pure planner against the
   current snapshot and never refreshes or writes broker state.
6. Inspect the plotted basis/quote/target/stop route, target-stop table,
   logical OCA grouping, rounded prices, and every blocking validation.

The first quick-hack workflow selects one conId directly rather than exposing a
browsable multi-position inventory. An ineligible or ambiguous selection stays
visible as a blocked state with its reason. Expanding this into an all-position
selector remains a follow-up within the desktop phase.

## Safety behavior

- The permanent banner states `READ-ONLY PREVIEW — ORDERS CANNOT BE SENT`.
- Only loopback hosts and `DU` paper-account IDs pass request construction.
- Unknown or false read-only, localhost-only, account, freshness, completion,
  contract-identity, quote, allocation, or market-rule state blocks a valid
  preview.
- Refresh starts by invalidating the prior snapshot; an error never restores
  cached broker state.
- Preview checks snapshot freshness again and becomes unavailable after expiry.
- Repeated preview actions are deterministic and cannot call the broker.
- Stop slippage and paper/live execution differences remain visible at all
  times.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

Qt tests run offscreen and cover the permanent safety notice, the absence of
order-action controls, blocked-refresh invalidation, connection-field
invalidation, repeated preview behavior, table rendering, and keyboard focus
flow. The production AST scan continues to reject forbidden IBKR order calls.

Paper behavior remains simulation evidence only and does not establish that
live stop or complex-order execution will behave identically.
