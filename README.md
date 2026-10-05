# IBKR Options Manager

Experimental, safety-first tooling for constructing protective stop and
profit-taking ladder orders for long single-leg options positions in
Interactive Brokers Trader Workstation (TWS).

The default mode is deliberately non-trading: connect to TWS in read-only mode,
display exact positions and working orders, and preview a validated laddered
OCA bracket plan for an open position. Coherent closing orders reserve their
remaining quantity regardless of origin; ambiguous orders block submission.

See [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md) for the current requirements
and safety constraints. The reviewed architecture, safety gates, delivery
slices, and acceptance criteria for the first milestone are in
[docs/MILESTONE_1_PLAN.md](docs/MILESTONE_1_PLAN.md).
The implemented module map and current safety seams are documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Slice 0 is complete with a dependency-light
[read-only paper-TWS capability probe](docs/READ_ONLY_PROBE.md). It deliberately
contains no order-placement, modification, cancellation, or exercise path.
The passing, redacted environment evidence is recorded in the
[compatibility note](docs/compatibility/TWS_10_50_1e_API_10_45_1.md).

Slice 1 is complete with a pure deterministic
[exit-plan engine](docs/PLANNER.md). The domain package produces preview-only
closing intents and contains no IBKR or GUI dependency.

Slice 2 is complete with an official-API
[read-only coherent snapshot adapter](docs/SNAPSHOTS.md). Its deterministic
offline suite and redacted paper-TWS smoke test pass.

Slice 3 now has a runnable PySide6 desktop shell containing a local
StarHTML/StarUI [desktop workbench](docs/DESKTOP_PREVIEW.md). It provides
position inventory, draft layer controls, outcome projection, and a
chronological order review.

To rehearse the desktop workbench without TWS or market data, start it with
deterministic simulated positions:

```sh
.venv/bin/ibkr-options-manager-gui --demo-data
```

The simulated-data header is deliberately prominent and the process never
opens a TWS connection or exposes order transmission.

## Paper execution (explicit opt-in)

The normal launch remains read-only. To expose the two-click paper submission
control, launch with:

```sh
.venv/bin/ibkr-options-manager-gui --account DU1234567 --enable-paper-execution
```

The desktop app remembers a paper account ID entered in Settings or the TWS
unavailable dialog. You can omit `--account` on later launches; an explicit
value overrides the remembered one. Without either, the dialog asks for a
paper account ID before any TWS connection attempt.

New bracket submission is intentionally blocked unless the fresh pre-send snapshot proves a
`DU` account, a loopback TWS connection, a complete/fresh position/contract/
quote/tick/order snapshot, API read-only mode explicitly **disabled**, and
coherent reservations for all existing selected-option closing orders.
Ungrouped SELL orders and complete target/stop OCA pairs reserve their verified
remaining quantity, including orders created outside this app. Only the
unreserved balance is eligible for new SELL LMT + SELL STP OCA pairs; changed
orders invalidate confirmation. For
journal-proven app-owned, complete active OCA pairs, **Sell now
(MKT)** is available as a separate two-confirmation paper-only experiment; it
cancels that layer's app-owned LMT/STP pair, verifies the cancellation, then
submits a standalone MKT without touching external orders. See
[paper-execution notes](docs/PAPER_EXECUTION.md).

Local verification from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```
