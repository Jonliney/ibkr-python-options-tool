# Trade dashboard and position analysis

The desktop opens on **Dashboard**. **Positions** opens the existing workbench;
each position has **Management** and **Analysis** views. A dashboard trade's
**View** button opens that specific entry, including a trade closed before this
app session. Multiple entries into the same contract have a trade selector.
Historical prices and rows never provide an execution-eligible broker snapshot.

```sh
.venv/bin/ibkr-options-manager-gui --demo-data
```

The standard dummy system seeds 13 deterministic examples in an isolated,
temporary SQLite database: eight matched closed trades, four existing demo
holdings, and one holding without reconstructable entry evidence. The examples
include losses, recoveries after a stop-level breach, a same-minute ambiguity,
missing history and several illustrative final-session bids. All data is labelled
test/illustrative. Restarting demo mode starts the same fixture afresh; it never
connects to TWS or writes into the brokerage-history database. Named execution
rehearsal scenarios and explicit `--con-id` launches still open Management.

## Dashboard

- Win rate counts positive and negative **gross matched closed-trade results**.
  Flat trades are shown separately and excluded from the percentage denominator.
  Open/partially closed, unknown and unreconciled results are not wins or losses.
- Gross realized P&L and the cumulative curve use only those closed-trade fills.
  The equity curve is ordered by completed trades, not marked-to-market equity.
- Average adverse excursion is the arithmetic mean of `min(0, worst return)`;
  average favorable excursion is the mean of `max(0, best return)`. Trades with
  no usable entry/price evidence or known history gaps are excluded, and the
  included trade count is shown. Observed prices may still be incomplete.
- Entry timing shows target reach, median dip before the first target touch,
  and how many target-reaching trades later returned to their entry price.
  A comparison at -10%, -20%, -30%, -40% and the selected stop shows which early
  dips subsequently recovered to the target. Trades that reached the target
  before dipping are excluded from that early-dip cohort. Each recovery row's
  denominator is trades hitting that level first, excluding same-minute ties.
- The funnel button beside Entry timing opens StarUI number inputs for stop
  and target. Results update in place after a short typing debounce, preserving
  focus and the open dropdown. Invalid inputs retain the last valid results;
  stale responses cannot replace a newer result. There is no Apply button.
- The median uses only unambiguous minima before the target minute; a lower low
  in the target minute could precede or follow the target, so that path is
  excluded from the median. Giveback counts require later observations, with
  same-minute ambiguities and missing follow-up excluded from its denominator.
  Sample counts accompany the values. All comparisons use observed bid paths
  through the last capture, not guaranteed fills or optimized strategy advice.
- All trades are shown without trade-state or currency filters. Monetary totals
  and equity curves remain separate per currency; percentage-only path statistics
  can be combined across currencies without adding unlike monetary amounts.

Dashboard controls use bundled StarUI buttons, dropdown positioning/dismissal,
inputs and labels. Semantic layout and tables are StarHTML, with custom CSS/SVG
charts. StarUI is the component library, not the renderer for every visual. Its
dropdown content uses dialog semantics for editable fields. The Lucide funnel
icon is bundled locally, with no runtime icon-network dependency.

## Position analysis

The selected entry shows entry price, actual closed gross P&L, the mean of minute
closes, worst/best observed return and dollar equivalents, price path, order of
extremes, stop/target sequence, matched fill events and durable position changes.
The chart uses elapsed UTC time and breaks its range/close paths across missing
minutes or session gaps. It never draws an observed price through an absent
interval. The entry reference is dashed; an observed exit marker is dotted.

A hypothetical gross value at bid `P` is:

`(P - weighted average entry premium) × total bought contracts × multiplier`

Return divides that amount by the total entry premium cost. All figures exclude
fees, FX, execution slippage, exercise and assignment. Actual results remain
separate from the counterfactual path, which continues after a sale through the
last captured observation. Do not interpret an observed bid as a guaranteed fill
for the full quantity.

The entry minute is excluded because it can contain prices before the fill. For
scale-ins, the full-size hypothetical path begins after the final buy minute and
is labelled accordingly. Actual cash P&L still uses all matched buy/sell fills.
Re-entries are separate flat-to-flat episodes; the contract's saved prices are
shared. Corrections replace their earlier execution revisions for analysis while
the database retains raw evidence. Leading sells, oversells and simultaneous
opposite-side fills lack an unambiguous supported long-trade history and are
withheld. A saved quantity that disagrees with reconstructed fills withholds the
trade result rather than inventing a closing execution.

A final-session bid is explicitly an estimate, not official settlement or proof
of an exercise outcome. `final_marks` holds such values with their source kind;
currently the demo provides examples. The live collector does not yet certify a
final trading-session boundary or populate these final marks automatically.
Absent evidence, hold-to-expiration remains unavailable rather than using an
arbitrary last quote. Expired IBKR option data is not guaranteed recoverable.

## Durable position history

Schema version 2 extends the collector database in place. Existing version-1
price bars, contracts, fills and observations are preserved. The latest existing
position observation is migrated into the saved position register.

- `portfolio_state` retains account/contract quantity, raw average cost, first
  and last observations and any observed transition to flat.
- `position_observations` records quantity/cost changes, including a zero quantity
  when a **complete verified** inventory no longer contains a previous holding.
- `portfolio_captures` records the timestamp of the last complete inventory.
  Partial, stale and disconnected observations cannot close saved positions.
- Contracts, executions, UTC execution times, prices and collection diagnostics
  remain in the same SQLite file. `final_marks` separates terminal-price evidence
  from ordinary bars.

A disappeared position establishes only an observed flat state. It does not
establish a sell price, fill, realized P&L or win. Unknown entries remain visible
in the journal. Data is isolated by account; reusing a contract ID across accounts
does not merge their trade results.

The desktop writes complete verified inventory observations and fills recovered
when a position is selected. The separate
[collector](TRADE_HISTORY_COLLECTOR.md) still handles continuous fill discovery and
minute-price backfill. Run it with the same database for full coverage; supply
its `--execution-timezone` when TWS times omit the login timezone. Collection and
broker-history limits remain visible in the analysis.

All historical observation/analysis data now belongs to this database. The
existing order-submission safety journal retains its established format and
independent fail-closed recovery behavior. No order authority or durable safety
state is migrated as a side effect of adding the dashboard.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

`tests/test_trade_analysis.py` verifies grouping, quantities, corrections,
scale-ins and sequencing. `tests/test_journal_dashboard.py` verifies migration,
closed-position persistence, account isolation, metrics, filters and navigation.
`tests/test_journal_browser.py` renders all three views at 1500px and the 1120px
minimum desktop width. GUI tests need Qt/WebEngine and loopback socket access.
The existing order-management and source-safety suites remain required.
