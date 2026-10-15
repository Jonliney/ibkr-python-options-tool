# Test suite audit — 15 October 2026

## Implemented follow-up — 16 October 2026

The audit below records the original suite. Its recommendations have now been
applied where supported by current product scope:

- The trailing matrix is **44 cases**: four manual-change cases (plain/limit ×
  amend/cancel), eight cancellation cases (plain/limit × four outcomes), and
  32 amendment cases. Six callback-race cases remain separate. This is smaller
  than the original conservative 52-case proposal because cancellation no
  longer varies an unused next-gap value.
- One bundled CSS selector case was replaced by a computed selected-toggle
  appearance check inside the existing browser smoke test. Incidental CSS,
  obsolete-copy and button-count assertions were removed from the large
  workbench form test; allocation, GTC, accessibility and local assets remain.
- Shared builders moved into `tests/support`. The former `test_app_demo.py`
  was split into `test_app_workbench.py`, `test_app_orders.py`,
  `test_app_trailing.py` and `test_app_browser.py`. Cross-test-module imports
  were removed. The `gui` marker separates the 46 Qt cases from HTTP/domain/
  transport tests without removing them from the full suite.
- Session-owned Qt state, per-test timers/window cleanup, workbench cleanup,
  temporary preferences/journals/traces and a shortened test heartbeat make
  the ordinary pytest command self-contained. Browser checks wait for observable
  readiness instead of fixed animation delays.
- The weak optional-import, saved-account loading and order-permutation tests
  now exercise their advertised behaviour. Domain guards inspect all imported
  aliases, relative imports and internal broker/app/execution boundaries; ASTs
  are parsed once per source file per run.
- **35 cases were added** for journal corruption/I/O failures, simultaneous
  confirmations, distinct-contract update preservation, cross-process locking,
  crash/restart behaviour, generated unresolved-submission lifecycles, connection
  boundaries, redaction, preference failures and architecture guards.
- The new tests reproduced duplicate transmission from simultaneous requests
  and acceptance of nonfinite timeouts. Journal updates now use reentrant
  per-path and cross-process locks, flush file/directory writes, and classify
  failed post-acknowledgement submission receipts as unknown outcomes. Timeout
  validation now rejects NaN/infinity. See `PAPER_EXECUTION.md` for assumptions.
- The 23 native-window cases remain because the architecture still describes
  that interface as retained. Retiring supported product code was not necessary
  to clean up the suite.

Net collection: **1,076 → 770 cases** (306 fewer, about 28%). Safety scenarios
were preserved while redundant dimensions were removed. Historical filenames
and line numbers in the original assessment below refer to the pre-refactor
tree.

Final verification on 16 October: the ordinary `.venv/bin/python -m pytest -q`
invocation (with duration/JUnit reporting) passed **770 cases plus three
subtests in 27.05 seconds**, with loopback permission for browser tests. No
external Qt bootstrap was needed. Mypy passed all 59 source files; Ruff and
`git diff --check` passed. Two existing dependency deprecation warnings remain.
The original complete run was 31.59 seconds; timings are single-run observations,
not a controlled performance benchmark.

## Assessment

The suite has 1,076 collected pytest cases, generated from 427 test functions
across 21 files (19,277 lines). The count alone overstates the problem:
384 cases come from a single Cartesian product. Most other parameter tables
represent distinct safety failures and should stay.

The main problems are redundant combinations, brittle presentation assertions,
test harness isolation, and missing durability/concurrency fault tests. Reducing
the number of cases is useful only when it reduces these costs without losing
an independently meaningful failure scenario.

The original audit covered its working tree, including the staged market-order
work. At that point no production code or existing tests were changed. Recommendations below
are source-based judgments, not measured branch or mutation coverage.

## Execution evidence

- Collection: `.venv/bin/python -m pytest --collect-only -q` → 1,076 cases.
- Non-window subset: `.venv/bin/python -m pytest -q
  --ignore=tests/test_app_demo.py --ignore=tests/test_app_window.py
  --durations=15 --junitxml=/tmp/ibkr-core-audit.xml` → 763 passed,
  three additional subtests passed, 3.78 seconds.
- The ordinary full-suite invocation encountered denied loopback binds in the
  restricted environment, then a fatal Python/Qt abort at
  `test_app_demo.py:4758` (`view.setHtml`). It did not finish.
- A full offline run with loopback permission, trace files redirected to `/tmp`,
  and a strongly retained `QApplication` passed all 1,076 cases, plus three
  subtests, in 31.59 seconds. Two dependency deprecation warnings remained.
  This confirms the tests pass under that harness; it does not establish that
  the documented bare pytest command is reliable.

The successful harness was:

```sh
IBKR_OPTIONS_MANAGER_CANCEL_TRACE=/tmp/ibkr-audit-cancellations.jsonl \
IBKR_OPTIONS_MANAGER_PRICE_TRACE=/tmp/ibkr-audit-prices.jsonl \
QT_QPA_PLATFORM=offscreen \
QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu' \
.venv/bin/python -c 'from PySide6.QtWidgets import QApplication; application = QApplication([]); import pytest; raise SystemExit(pytest.main(["-q", "--durations=25", "--junitxml=/tmp/ibkr-full-audit.xml"]))'
```

JUnit per-case duration sums include setup/teardown, have rounding error, and
are not equivalent to total wall time. In this run:

| Area | Cases | Summed duration |
| --- | ---: | ---: |
| Web workbench / demo | 290 | 26.32 s |
| Execution service and transport | 506 | 1.83 s |
| Native window | 23 | 0.81 s |
| Bracket transmission | 43 | 0.63 s |
| Source safety | 7 | 0.39 s |
| Remaining files | 207 | 0.38 s |

The 384-case matrix itself takes approximately 1.58 seconds. Its reduction is
primarily a maintenance and diagnostic improvement, not the largest speedup.

## Highest-value reductions

### 1. Replace the 384-case trailing-change product with explicit scenarios

`tests/test_execution.py:526–740` crosses trail unit × cancellation × cancellation
callback × rejection × manual gap change × old/new gap shape:
`4 × 2 × 3 × 2 × 2 × 4 = 384`.

There are demonstrably irrelevant dimensions:

- With `manual_gap_change=True`, verification blocks before any write. The
  requested amendment, rejection callback and cancellation callback do not run.
- With cancellation, the requested trail unit and next limit gap are unused.
- With amendment, the ordinary cancellation-event values (`status`, `202`,
  `both`) do not affect `placeOrder`.
- With rejection during cancellation, the fake returns before using the
  cancellation-event value.

A conservative source-derived starting table is **52 cases**:

| Scenario family | Cases |
| --- | ---: |
| Manual gap change, retaining the four original gap transitions | 4 |
| Cancellation, four original gap transitions × three acknowledgement paths plus rejection | 16 |
| Amendment, four gap transitions × four trail-unit/ack modes × accepted/rejected | 32 |

Some of those 52 still overlap. Start here, preserve the six separately collected
cancellation race cases at `test_execution.py:747`, and validate the replacement
against the transport branches and assertions before deleting the old product.
Extract a helper: the race test currently directly calls another test function.

This one change would remove 332 cases, reducing the suite to **744** before any
other additions or deletions. That is an estimate for a proposed refactor, not
a completed or coverage-proven reduction.

### 2. Retire native-window coverage only with the native surface

All 23 cases in `tests/test_app_window.py` exercise `app.window.PlannerWindow`.
The shipped launcher constructs `StarUIPlannerWindow` instead
(`app/main.py:145`); no production Python caller of `PlannerWindow` was found.
However, `docs/ARCHITECTURE.md` explicitly describes it as a retained surface.

If the native surface is no longer supported, remove its implementation and
tests together, after checking that quantity allocation and invalidation
behaviour have equivalent coverage in the current web surface. Do not silently
delete tests while continuing to claim support. This is a conditional additional
23-case reduction, to 721 after the matrix change.

### 3. Remove presentation archaeology from current UI tests

`test_starui_workbench_renders_and_adds_a_layer_from_a_server_owned_form`
(`test_app_demo.py:6094`) combines real allocation/form behaviour with exact CSS
class strings, grid widths, old-copy absence and an exact count of Add Layer
buttons. Those incidental assertions make harmless redesigns expensive.

Remove assertions such as exact `grid-template-columns`, class ordering,
`max-width: 80rem`, and the absence of superseded labels. Retain user-visible
allocation, valid field values, GTC enforcement, accessibility associations,
submission controls, and local-asset loading. Put layout checks in a small
browser-level test if that layout is an explicit supported requirement.

The standalone bundled CSS selector test at `test_app_demo.py:15` is a candidate
for replacement by a selected-toggle appearance check in an existing browser
smoke test. Deleting it without behavioural replacement would lose coverage of
the original styling regression.

Do not replace these assertions with giant HTML snapshots: that preserves the
same maintenance problem in a different form.

## Runtime and harness improvements

1. **Own Qt lifetime in a session fixture.** Several tests use
   `QApplication.instance() or QApplication([])` without retaining the result,
   including `test_app_demo.py:4692`. Introduce one strongly held session
   application and deterministic window/server/workbench cleanup. The observed
   abort and successful retained-application run make lifetime management a
   concrete concern; the precise abort cause was not separately isolated.
2. **Isolate state globally.** Tests currently attempt to append traces under
   the normal Application Support directory. Redirect both trace paths into
   test temporary storage and provide temporary preferences/journal defaults.
   Keep state per test rather than sharing mutable workbenches for speed.
3. **Control heartbeat time in server tests.** The direct SSE heartbeat test
   takes 2.03 s and the embedded observer indicator test takes 2.45 s. The server
   generator sleeps for two seconds (`app/web/surface.py:423`). Inject the
   heartbeat wait/interval for deterministic server tests; retain one real
   browser observer-disconnect smoke test with a shortened test interval.
4. **Use observable readiness instead of fixed browser delays.** Existing
   browser tests contain 100/150/250 ms delays and 5–10 s deadline timers. Keep
   bounded deadlines, but wait for the relevant element/state and cancel timer
   objects on teardown so callbacks cannot spill into later tests.
5. **Split test layers with strict registered markers.** Keep domain, journal,
   transport and HTTP tests in the fast default development selection; give
   actual Qt/WebEngine tests their own selectable group. Run both groups for
   release verification. `test_app_demo.py` mixes all these levels today.
6. **Extract shared test builders.** Execution, bracket, market, trailing and
   UI tests import fixtures from other test modules, including reciprocal
   imports between execution and bracket tests. Move data builders/fakes into
   explicit test support modules. Split the 9,548-line demo test file by
   workflow; file splitting itself does not reduce runtime or coverage.
7. **Avoid premature parallelism.** Shared Qt state, profile interceptors,
   trace paths and listener slots need isolation first. Core tests already
   finish in under four seconds. Parsing production ASTs once instead of
   repeatedly is a lower-priority optimisation (~0.39 s total source tests).

## Tests that should be strengthened or added

### Safety priorities

1. **Journal fault injection.** Add truncated JSON, wrong top-level/entry shapes,
   read denial, temporary-file write failure and `os.replace` failure. Assert
   no transport call when PREPARED cannot be persisted; preserve a blocking
   intent when transmission succeeded but recording the receipt failed; restart
   and verify no resend. Existing tests exercise successful persistence and
   transport failures, not these filesystem failure boundaries. `_write`
   (`execution.py:2248`) uses a fixed `.tmp` name and replacement; replacement
   alone is not evidence of power-loss durability.
2. **Simultaneous duplicate requests.** Existing duplicate tests are sequential.
   Use barriers to race two confirmations/services against the same journal;
   require at most one transport invocation and no lost journal entries.
   Include two application instances, or explicitly reject the second instance
   if that is the supported contract. The client lease is process-local and
   does not itself prove journal transaction safety across instances.
3. **Generated lifecycle sequences.** Extend the two useful planner Hypothesis
   tests with a bounded execution state machine covering submit, partial fill,
   manual edit/cancel, disconnect, recovery and restart. Assert ownership,
   allocation conservation and unresolved-outcome locks after every step.
   Start with focused journal/concurrency tests; do not introduce another large
   Cartesian product.
4. **Stronger architecture guards.** The domain dependency test currently checks
   only imports whose first component is `ibapi` or `PySide6`; it does not catch
   a domain import of an internal broker/app module, or all aliases in a
   multi-module `import`. Check internal layer boundaries too. The cancel AST
   test's `{None}` argument-shape assertion cannot establish ownership: retain
   the behavioural external-order rejection tests as the ownership proof.

### Small, concrete correctness gaps

- `test_order_callback_sequence_and_unrelated_orders_do_not_change_fingerprint`
  (`test_planner.py:996`) only compares an empty order set with one unrelated
  order. Add a real permutation of multiple relevant working orders; the
  current name claims more than the body verifies.
- `test_module_is_importable_without_ibapi_installed` (`test_ibkr_probe.py:11`)
  merely instantiates an already imported class. Use a fresh subprocess with
  IBAPI imports deliberately blocked to establish the advertised guarantee.
- `test_non_paper_account_is_neither_saved_nor_loaded`
  (`test_account_preferences.py:20`) rejects saving a live account, then reads
  empty preferences. Seed a live-account value directly and verify loading
  rejects it. Also cover non-string stored values and a settings sync error.
- Add a compact shared connection-validation table for invalid port boundaries,
  empty account, nonpositive and nonfinite timeout values. Existing constructor
  tests cover several account/host/client barriers, but not the complete common
  validation contract.
- Add direct redaction cases for overlapping IDs, repeated IDs and short IDs.
  Existing CLI/view-model tests cover normal redaction; keep those boundary
  integrations rather than duplicating all redaction cases at every layer.

These are gaps identified by inspecting source/tests. No mutation analysis or
line/branch coverage run was performed, and no claim is made that each gap is
an existing production bug.

## File-by-file disposition

| File under `tests/` | Cases | Recommendation |
| --- | ---: | --- |
| `test_account_preferences.py` | 2 | Keep; strengthen rejected-load and sync-failure evidence. |
| `test_app_demo.py` | 290 | Keep workflows; split by layer/workflow, trim incidental markup assertions, fix Qt/state/timing fixtures. |
| `test_app_view_model.py` | 10 | Keep; current web launcher still uses this view model. |
| `test_app_window.py` | 23 | Conditional retirement with unused native surface; otherwise keep. |
| `test_bracket_transmission.py` | 43 | Keep stop-first, one-sided failure, exact field and callback-race coverage. |
| `test_broker_snapshot_capture.py` | 11 | Keep callback normalization, P&L, identity preference and quote fallback coverage. |
| `test_capability.py` | 4 | Keep; read-only probing remains a supported CLI path. |
| `test_cli.py` | 4 | Keep CLI defaults/redaction integration. |
| `test_execution.py` | 506 | Replace 384-case product; retain journal, ownership, restart, partial-fill and write-barrier scenarios. |
| `test_ibkr_probe.py` | 2 | Keep settings extraction; replace weak optional-import test. |
| `test_market_orders.py` | 33 | Keep; current staged feature, with distinct ownership, cancellation-race and outcome cases. |
| `test_observation.py` | 3 | Keep; prefer explicit event synchronisation over polling loops. |
| `test_order_observation.py` | 4 | Keep client lease/handoff tests; add journal-level concurrent submission separately. |
| `test_outcome.py` | 3 | Keep cheap whole-position accounting invariants. |
| `test_planner.py` | 61 | Keep including both property tests; fix missing actual permutation scenario. |
| `test_portfolio.py` | 7 | Keep inventory eligibility, ordering, identity and stale-state contracts. |
| `test_position_observation.py` | 3 | Keep cheap pure notice/baseline tests. |
| `test_price_update_trace.py` | 1 | Keep private-file and rotation behaviour. |
| `test_snapshot.py` | 10 | Keep capture completion, exact identity, freshness and invalidation contracts. |
| `test_source_safety.py` | 7 | Keep as supplementary guards; strengthen internal domain import detection. |
| `test_trailing_outcome.py` | 49 | Keep: parameter tables distinguish actual evidence defects, not irrelevant crossed dimensions. |

## Recommended sequence

1. Fix harness ownership, trace isolation and cleanup so the documented command
   works independently of test order.
2. Replace the trailing matrix with named behavioural scenarios; keep race tests.
3. Add journal I/O and simultaneous-submit tests before broader restructuring.
4. Remove incidental UI assertions and control heartbeat timing.
5. Decide whether the retained native surface is supported; remove code and
   tests together if retired.

Do not set an arbitrary target such as 500 tests. A suite around 744 cases with
clearer evidence, followed by a small number of high-value additions, would be
healthier than either the current inflated matrix or a smaller suite that lost
ownership, quantity, rounding, partial-fill, rejection and recovery coverage.
