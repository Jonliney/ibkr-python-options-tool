# IBKR Options Manager

Experimental, safety-first tooling for constructing protective stop and
profit-taking ladder orders for long single-leg options positions in
Interactive Brokers Trader Workstation (TWS).

The initial milestone is deliberately non-trading: connect to TWS in
read-only mode, display exact positions and working orders, and preview a
validated exit plan. Live order transmission is out of scope until the plan
engine and paper-trading workflow have been independently verified.

See [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md) for the current requirements
and safety constraints. The reviewed architecture, safety gates, delivery
slices, and acceptance criteria for the first milestone are in
[docs/MILESTONE_1_PLAN.md](docs/MILESTONE_1_PLAN.md).

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

Slice 3 now has a runnable PySide6
[read-only desktop preview](docs/DESKTOP_PREVIEW.md) with connection evidence,
exact-position verification, plan controls, a plotted price route, target/stop
pairs, and blocking validations. It remains structurally unable to send an
order.

Local verification from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```
