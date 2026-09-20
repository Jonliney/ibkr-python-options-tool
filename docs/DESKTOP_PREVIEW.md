# Read-only desktop preview

Slice 3 provides one PySide6 desktop shell containing a locally served,
embedded StarHTML/StarUI workbench. It refreshes a coherent paper-TWS snapshot
and rebuilds the pure exit-plan preview. The loopback web server exposes only
the local workbench during the process lifetime; it has no arm, confirm,
submit, modify, cancel, bind, exercise, or global-cancel control or transport
path. The WebView also rejects every request whose destination is not its own
`127.0.0.1` server.

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
states `SIMULATED DATA`; this mode neither connects to TWS nor sends, modifies,
or cancels an order. Optional `--con-id` values must be one of the contracts
present in the simulated data.

The account must use the project's `DU` paper-account allowlist convention.
The exact ID is kept only in memory for the current process. The evidence view
redacts it, and changing any connection-selection field immediately clears the
visible plan and requires another refresh.

## Workflow

1. Verify the literal loopback endpoint, paper port, nonzero client ID, and
   exact paper account.
2. Select **Refresh**, which invalidates the previous snapshot before the
   read begins. The first returned option is selected by default.
3. Choose an eligible position from the persistent long-position inventory.
   Associated open orders are prominently called out; only the verified
   unassociated quantity can be drafted.
4. Use **Draft layers** to adjust each layer's target percentage, stop-loss
   percentage, quantity, and TIF. Dollar prices are derived from cost basis
   using the verified market rule. **Equal split** distributes every verified
   available contract across the current rows.
5. Review the expected gain, maximum loss, breakeven threshold, and the
   chronological sell-limit/sell-stop action review.
6. Select **Preview current draft**. It re-runs only the pure planner against
   the current verified snapshot and never refreshes or writes broker state.

**Active layers** is intentionally labelled as unavailable: management of
submitted brackets belongs to a later, explicitly authorised transmission
milestone. No UI control suggests it can modify an existing order.

## Safety behavior

- The action review states that no order will be placed, modified, or
  cancelled, and **Transmission locked** remains disabled.
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

The deterministic web-surface tests cover inventory rendering, draft-layer
addition/removal, previewing, and the permanent transmission lock. The
production AST scan continues to reject forbidden IBKR order calls.

Paper behavior remains simulation evidence only and does not establish that
live stop or complex-order execution will behave identically.
