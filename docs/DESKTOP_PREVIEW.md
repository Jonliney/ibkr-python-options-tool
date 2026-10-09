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

The desktop app remembers a paper account ID entered in Settings or the TWS
unavailable dialog. On later launches, `--account` is optional; an explicit
`--account` takes precedence. If no paper account ID is available, the app opens
the TWS unavailable dialog and asks for one before attempting a connection.
Only the account identifier is saved locally, not TWS credentials.

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

With `--enable-paper-execution`, simulated acknowledgements are saved in a
separate demo journal. Later demo snapshots reconstruct acknowledged bracket
orders from that journal, including after a restart, so they can be verified
and managed in the interface. To rehearse a stop amendment without TWS, run:

```sh
.venv/bin/ibkr-options-manager-gui --demo-data --enable-paper-execution --con-id 1003625093
```

Build and confirm a SPY bracket, then set its active stop return to `0%` and
confirm the amendment. The demo acknowledges the exact amended legs and the
next simulated read shows the new stop price. The app labels that result as
simulated; it does not establish how TWS will acknowledge or fill the order.
For a repeatable check with a fresh temporary demo journal, run
`.venv/bin/python -m pytest -q tests/test_app_demo.py -k demo_weekend_stop_to_break_even_uses_acknowledged_price_update`.
The desktop demo journal itself persists across launches, so prior order
attempts remain visible. It is separate from paper TWS history. A one-time
NVDA example starts with three contracts awaiting
verification, leaving four of seven available. Confirming that its orders do
not exist clears it permanently from the demo journal. An unresolved layer
keeps a Verify action: finding both exact orders
makes it active, while confirming neither exists requires a fresh, complete
order and execution check before the reserved quantity is released.

### Trailing workflow examples

Named examples use **simulated execution automatically**, never connect to TWS,
and use a fresh disposable journal for each launch. Close and relaunch to reset
one. Run any of these from the repository root; TSLA is selected automatically.
All prices, orders, fills, and P&L values below are illustrative.

| Scenario | Command suffix after `--demo-data --demo-scenario` | What to inspect |
| --- | --- | --- |
| Convert an existing bracket | `convert` | TSLA has a 2-contract app-owned bracket and 1 unassigned contract. Open **Convert entire position**, enter a `$0.25` trail and `$0.10` limit offset, then review the 3-contract action and its risk. Confirming only changes this disposable demo. |
| Working trailing limit | `working-trail` | TSLA has a 3-contract `TRAIL LIMIT` order with initial stop `$8.80` and offset `$0.10`. Inspect the active trailing row. |
| Partial trailing fill | `partial-fill` | A synthetic 2-contract sale at `$9.10` leaves 1 held contract and 1 working trailing contract. Compare the position, row quantity, and realised P&L display. |
| Manual cancellation | `manual-cancel` | The app journal still owns a 3-contract trailing limit, but the simulated broker no longer reports its working order. Inspect the unresolved row and available-quantity treatment. |
| Fully closed position | `closed-trail` | Start with 3 held contracts and a working trail. Press **Refresh** once to simulate a 3-contract fill, then select TSLA from the closed-position list. Inspect the closed workspace and realised P&L. |
| Missing option bid | `no-bid` | TSLA has no bid. Open **Convert entire position** and review a trail to exercise the blocked message without an order. |

For example:

```sh
.venv/bin/ibkr-options-manager-gui --demo-data --demo-scenario convert
.venv/bin/ibkr-options-manager-gui --demo-data --demo-scenario closed-trail
```

These scenarios expose unfinished follow-ups deliberately: trailing fills are
not yet reconciled into realised P&L, a closed trailing row is not yet retained,
and a manually cancelled trailing order currently shows `CHECK TWS`. The
synthetic fill amounts are evidence for UI iteration only; they do not imply
broker-verified net P&L. Use one scenario per app launch so the expected state
is unambiguous.

The account must use the project's `DU` paper-account allowlist convention.
The exact ID is kept only in memory for the current process. The evidence view
redacts it, and changing any connection-selection field immediately clears the
visible plan and requires another refresh.

The selected-contract header uses verified contract fields for its readable
name and the latest selected snapshot for open quantity, average option price,
bid, and ask. The app header separately identifies connection, new-layer
availability when relevant, and paper-execution mode. It does not show IBKR's
market-data classification as a live-feed claim: prices are from the last
verified snapshot. The displayed total is the open quantity
plus fully reconciled app-recorded sold layers; it cannot reconstruct external
or manual sales. Realised P&L includes only those app-recorded exits. A partial
or uncertain outcome leaves the affected total or P&L unavailable rather than
displaying a misleading zero.

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
- Removing a draft layer preserves the quantities entered in surviving rows,
  even when the form temporarily totals more than the available position.
  Paper execution remains blocked until every available contract is assigned
  to a draft layer; deletion never redistributes contracts automatically.
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
