# Current architecture

This document describes the implemented architecture. Historical milestone
documents explain how it evolved; this map is the source of truth for current
module ownership and safety seams.

## Runtime paths

The default path is observation-only:

```text
desktop surface
  -> PlannerViewModel
    -> PortfolioCoordinator / SnapshotCoordinator
      -> ReadOnlyBroker interface
        -> IbkrSnapshotBroker adapter
    -> build_exit_plan(snapshot, request)
```

Paper writes exist behind the explicit `--enable-paper-execution` launch flag:

```text
desktop surface confirmation
  -> PlannerViewModel fresh snapshot + deterministic plan
  -> PaperExecutionService
    -> ExecutionJournal (durable ownership and duplicate suppression)
    -> paper transport interface
      -> IbkrPaperExecutionBroker adapter
```

No domain module imports a GUI or IBKR transport. The desktop surfaces never
construct IBKR contracts or orders.

## Modules and interfaces

| Module | Interface | Responsibility |
| --- | --- | --- |
| `domain.planner` | `build_exit_plan`, price preview and rounding | Deep, deterministic planning module. It validates exact contract identity, allocation, quantities, prices, ticks, and logical OCA pairs without I/O. |
| `broker.read_only` | request/capture values plus observation-only protocols | Transport seam for bounded portfolio and selected-contract captures. It deliberately contains no write method. |
| `broker.ibkr` | `IbkrSnapshotBroker.capture` | Official-API read adapter. It collects callback evidence and returns an immutable capture. |
| `broker.observations` | `working_orders_from_capture` | Normalizes and deterministically orders broker observations once for both portfolio and selected-contract publishers. |
| `portfolio` | `PortfolioCoordinator.refresh/current` | Publishes only a coherent option inventory and expires cached inventory by monotonic age. |
| `snapshot` | `SnapshotCoordinator.refresh/current` | Resolves one exact option and publishes only a complete coherent domain snapshot. |
| `app.view_model` | presentation commands returning immutable `ViewState` | Owns UI state transitions, redaction, snapshot invalidation, form parsing, and planner invocation. A failed or stale selected-position result clears the cached snapshot. |
| `app.web.surface` | local StarHTML workbench | Primary desktop surface. It renders state and stages explicit paper actions but does not talk to IBKR directly. |
| `app.window` | PySide workbench | Retained native surface using the same view-model interface. |
| `execution` | `PaperExecutionService` and `ExecutionJournal` | Re-verifies ownership and safety, journals before writes, rejects automatic retry after an unknown outcome, and exposes narrow management operations. |
| `broker.execution` | paper transport interfaces | The only module allowed to call IBKR order placement or cancellation methods. |
| `connection` | `validate_paper_connection` | One connection envelope for every paper-TWS reader: literal loopback, positive nonzero client ID, valid port and timeout, and a `DU` account. |
| `redaction` | `redact_account`, `redact_accounts` | One account-redaction rule for broker errors, CLI output, and UI state. |

## Safety invariants

- Ordinary launch is read-only. Paper execution requires an explicit process
  flag and TWS must explicitly report API read-only mode disabled.
- Account plus IBKR `conId` is identity. Display symbols are evidence only.
- Every refresh invalidates prior evidence before transport work begins.
- Missing barriers, ambiguity, stale observations, disconnects, invalid ticks,
  unsupported order shapes, or incomplete acknowledgements block the action.
- Planning is pure and returns intents; it cannot transmit orders.
- Paper management accepts only complete, journal-proven, app-owned OCA pairs
  created by the configured API client.
- The isolated writer never disables TWS precautions, uses global cancel, or
  modifies an order the journal does not prove the application owns.
- A write is journaled before transport starts. An uncertain acknowledgement
  becomes non-retryable pending broker reconciliation or manual investigation.

## Deliberate seams

The `ReadOnlyBroker` and paper transport protocols are real seams: production
IBKR adapters and deterministic demo/test adapters both satisfy them. Planner
helpers remain internal because exposing them would enlarge the interface
without giving callers useful leverage.

The two desktop surfaces share the view-model interface. Their rendering code
is intentionally separate because the widget and HTML runtimes have different
lifecycle models. Pure behavior should move below that seam when both surfaces
need it; visual construction should remain local to each adapter.

## Deferred restructuring

`app.web.surface`, `app.window`, and `broker.execution` are large implementations.
Splitting them by line count alone would create shallow modules and obscure the
safety sequence. Extract a module only when it can own a complete state
transition or transport workflow behind a smaller interface, with observable
behavior covered at that interface first.

## Verification

Run from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

The embedded-webview regression test binds an ephemeral loopback port. It must
run in an environment that permits local socket binding.

The WebView rejects non-loopback requests. StarHTML's positioning plugin must
therefore load its Floating UI dependency from packaged local assets. A failed
JavaScript module import prevents reactive controls such as Settings from
initializing, even while native form buttons still work. The desktop regression
test uses the same request filter as the application. UI availability does not
relax server-side paper-account, fresh-state, or order-ownership checks.
