# Milestone 1 plan: read-only exit planner

Status: in progress — Slices 0 and 1 complete  
Reviewed: 2026-09-16  
Scope: paper TWS only; no order transmission, modification, cancellation, or
option exercise

## Outcome

Milestone 1 will be a local desktop application that reads a fully qualified,
fresh snapshot from paper TWS and produces a deterministic preview of closing
order intents for an existing long, single-leg option position. The preview
will show every input, transformation, validation, warning, and blocking
condition. It will contain no path that can transmit or modify an order.

The milestone is complete only if the application can prove all of the
following for the selected position:

- the connection is loopback-only, the configured paper account is the only
  selected account, and TWS reports that API read-only mode is enabled;
- the position, exact contract details, open orders, market rule, quote type,
  and connection state form a complete snapshot;
- the contract is an existing positive `OPT` position and every identity field
  matches the position and contract-details responses;
- existing closing orders are visible well enough to calculate a conservative
  allocated quantity;
- every proposed action is a closing `SELL`, total planned quantity is no more
  than the unallocated position, and every target has equal stop coverage;
- the same semantic inputs produce byte-for-byte equivalent plan content and
  the same plan fingerprint.

Any unknown, ambiguous, stale, conflicting, or unsupported value yields a
`BLOCKED` preview. There is no override.

## Design review

### What is sound

The proposed independent OCA-pair model is a good fit for tranche exits. One
target and one stop share the same quantity and logical OCA group, so a fill on
one side reduces or removes only that tranche's other side. The intended later
IBKR setting is OCA type 2: proportional reduction **with block**. IBKR
documents that the “with block” variants provide overfill protection by routing
only one order in the group at a time.

Using position cost rather than a transient quote as the percentage basis is
also sound. The quote remains important context and a freshness signal, but it
must not silently replace the cost basis.

Separating a pure planner from TWS transport and the GUI is the right safety
shape. It allows quantities, prices, rounding, and invariants to be tested
without a broker session.

### Corrections and decisions required

1. **Read-only order discovery is a release gate.** IBKR documents that
   `reqOpenOrders()` binds manually submitted TWS orders when client ID 0 is
   used and rejects that call in read-only mode. Milestone 1 must therefore use
   a nonzero client ID, must never call `reqOpenOrders()` or
   `reqAutoOpenOrders()`, and may use only `reqAllOpenOrders()` for an order
   snapshot. Before normal implementation, an integration spike must prove on
   the pinned paper TWS/API versions that `reqAllOpenOrders()` works while
   read-only is enabled and includes the manually entered orders needed for
   allocation. If it does not, the current requirements are incompatible; do
   not disable read-only or infer that there are no orders.

2. **“Paper/live” cannot be inferred from a default port alone.** TWS ports are
   configurable. The application should have a fixed loopback host, a locally
   configured allowlist containing the exact paper account ID, an expected
   paper port, and a displayed environment attestation. An account or setting
   mismatch blocks planning. A `DU` prefix can be shown as corroborating
   evidence, not treated as the sole proof.

3. **Read-only mode should be observed, not assumed.** With a pinned API that
   supports settings retrieval, read `readOnlyApi` and require `true`. If the
   setting cannot be read in a read-only session, the capability spike must
   record that limitation and the application must not label the connection
   verified.

4. **Open orders are a snapshot, not a subscription.** A plan must record the
   open-order snapshot completion time and expire when its configured TTL is
   exceeded. Explicit refresh obtains a new snapshot. Any disconnect,
   reconnect, request error, account change, position update, or observed order
   change invalidates the plan immediately.

5. **Allocated quantity is not the sum of all sell legs.** For orders on the
   selected account and contract, an ungrouped closing sell consumes its
   remaining quantity. Within a recognizable same-contract OCA group, the
   conservative executable quantity is the maximum remaining sell quantity,
   not the sum of mutually exclusive legs. Unknown order relationships,
   incomplete status, mixed contracts in an OCA group, inconsistent pair
   quantities, or a computed allocation greater than the position block the
   preview.

6. **The TWS position cost needs normalization and verification.** The domain
   needs a per-option premium basis, while derivative cost fields can be
   multiplier-adjusted. The adapter must normalize the observed value with the
   verified contract multiplier and an integration fixture must compare the
   result with the value displayed in TWS. A missing, zero, non-integral, or
   inconsistent multiplier blocks planning. No quote-derived fallback is
   allowed.

7. **Remainders need a required policy, not an implicit rule.** Support two
   explicit policies initially:

   - `NEXT_RUNG`: create a smaller final target/stop pair at the next target;
   - `ADD_TO_LAST`: add the remainder to the last full target/stop pair.

   No “leave unprotected” policy belongs in this product. If there are fewer
   contracts than the tranche size, `NEXT_RUNG` creates one smaller first pair;
   `ADD_TO_LAST` is invalid because no full tranche exists.

8. **Rounding must be side- and purpose-aware.** Use decimal arithmetic only.
   Match the selected order exchange to the corresponding market rule from
   contract details; missing or ambiguous mapping blocks the plan. Round a
   sell limit target upward so it does not realize less profit than configured.
   Round a sell stop trigger upward so it does not protect at a lower price
   than configured. Re-evaluate the market-rule band after rounding, since an
   increment can change at a price edge.

9. **Stop trigger behavior follows TWS defaults.** Trigger method is a property
   of an individual simulated stop, not its OCA group. Milestone 1 does not
   expose or override it; a later execution milestone must preserve the
   broker's default behaviour and verify its execution semantics in paper TWS.

10. **Milestone 1 cannot yet prove submission idempotency.** It can and should
    prove deterministic plan generation and assign a semantic plan fingerprint.
    Persistence, order ownership, submission acknowledgements, and duplicate
    suppression remain later-milestone work. No “Confirm” control should exist
    in Milestone 1.

## Recommended implementation shape

Use Python 3.12, the official IBKR Python TWS API installed from a pinned IBKR
download, PySide6 for a thin desktop GUI, `pytest` plus Hypothesis for tests,
and `Decimal` for all domain quantities and prices. Pin TWS and TWS API to the
same tested release. As of 2026-09-16, IBKR lists TWS API 10.50 as the latest
release (released 2026-09-09), including the Python API on macOS/Unix. Its
download is gated by IBKR's license acceptance, so the user must review and
accept those terms before the dependency is installed or vendored.

The main external seam is intentionally narrow:

```text
PySide6 views
    -> SnapshotCoordinator
        -> ReadOnlyBroker interface
            -> IbkrReadOnlyAdapter
            -> FakeReadOnlyBroker (tests)
    -> build_exit_plan(snapshot, request) -> PlanResult
```

### Modules and interfaces

| Module | Interface | Responsibility |
| --- | --- | --- |
| `domain.model` | immutable value types | Account, contract identity, position, quote, market rule, working order, snapshot, plan request, validation, and order intent. No IBKR or GUI types cross this seam. |
| `domain.planner` | `build_exit_plan(snapshot, request) -> PlanResult` | A deep, pure module containing eligibility, allocation, tranche construction, remainder handling, pricing, rounding, validation, logical OCA grouping, and fingerprinting. It never reads clocks, files, sockets, or environment variables. |
| `broker.read_only` | observation methods only | Defines requests for settings, managed accounts, positions, contract details, all open orders, market data, market rules, and server time. It deliberately has no place, modify, cancel, exercise, or global-cancel method. |
| `broker.ibkr` | `ReadOnlyBroker` adapter | Converts IBKR callbacks to typed observations. It contains the only dependency on `ibapi` and converts binary floats to decimal strings before values enter the domain. |
| `snapshot.coordinator` | `refresh()`, `current()` | Correlates request IDs and completion barriers, tracks monotonic receive times and errors, and publishes one immutable `BrokerSnapshot` only when required feeds complete. Invalidates on state changes. |
| `app.view_model` | presentation state and commands | Maps snapshots and plan results to GUI state. It cannot access the IBKR client directly. |
| `app.gui` | view rendering | Shows connection evidence, eligible positions, input controls, plan pairs, raw and rounded prices, allocation, validation results, and prominent `PREVIEW ONLY` status. |

The planner's interface is the primary test surface. Allocation and rounding can
remain internal seams unless independent reuse appears; making each helper a
public module would create shallow interfaces without added leverage.

### Domain values

At minimum, use explicit immutable types for:

- `ContractKey(account, con_id)`;
- `VerifiedOptionContract(con_id, sec_type, expiry, strike, right, multiplier,
  currency, trading_class, exchange, local_symbol)`;
- `ObservedPosition(quantity, raw_average_cost, unit_basis)`;
- `WorkingOrder(perm_id, client_id, order_id, account, con_id, action,
  order_type, remaining, status, oca_group, parent_id, observed_at)`;
- `Quote(bid, ask, last, close, market_data_type, observed_at)`;
- `MarketRule(exchange, bands, observed_at)`;
- `BrokerSnapshot(completion_times, connection_epoch, errors, ...)`;
- `PlanRequest(tranche_size, target_percentages, stop_loss_percentage,
  remainder_policy, tif)`;
- `OrderIntent(action, order_type, quantity, raw_price, rounded_price, tif,
  logical_oca_group)`;
- `PlanResult(status, fingerprint, allocated, available, pairs, validations)`.

Never use display labels, symbols, or local symbols as identity. The selected
identity is account plus `conId`; every descriptive field is verified and
displayed as evidence.

## Delivery slices

### Slice 0 — prove the read-only contract (complete 2026-09-16)

Build a disposable, non-trading connection probe against paper TWS with a
nonzero client ID. It may call only read methods.

Verify and record:

- connection succeeds only via `127.0.0.1` or `::1`;
- settings retrieval reports `readOnlyApi=true`;
- the exact configured paper account appears and no live account is selected;
- `reqPositions`, `reqContractDetails`, `reqMktData`, and `reqMarketRule`
  complete in read-only mode;
- `reqAllOpenOrders` completes in read-only mode and sees a manually entered
  paper-TWS option order without binding or modifying it;
- no `reqOpenOrders`, `reqAutoOpenOrders`, `placeOrder`, `cancelOrder`,
  `reqGlobalCancel`, or `exerciseOptions` message is emitted.

Exit: a versioned compatibility note and recorded, redacted callback fixture.
If manual-order visibility or read-only verification fails, stop the milestone
and revise the requirements; do not proceed with a falsely safe allocator.

Result: passed against paper TWS `10.50.1e` (server version `223`) using the
official Python API package `10.45.1`. See the
[compatibility note](compatibility/TWS_10_50_1e_API_10_45_1.md) and
[redacted fixture](../tests/fixtures/read_only_probe_pass.json). The observed
version skew is accepted only for the read-only calls proven by this fixture.

### Slice 1 — scaffold and pure planner (complete 2026-09-16)

- Establish the `src/` package layout, dependency lock, type checking, linting,
  and tests.
- Implement immutable domain values and `build_exit_plan` without IBKR types.
- Implement exact contract eligibility, conservative allocation, tranche and
  remainder policies, unit-basis prices, market-rule rounding, validation
  aggregation, and canonical fingerprinting.
- Use logical OCA IDs such as `plan-short-hash/tranche-index`; do not create
  IBKR `Order` objects.

Exit: exhaustive examples and property tests pass with no network or TWS.

Result: complete. The deep planner module is exposed only through
`build_exit_plan(snapshot, request) -> PlanResult`; immutable domain values,
allocation, both remainder policies, directional market-rule rounding,
fail-closed validation, logical OCA pairs, and canonical fingerprinting are
implemented without IBKR or GUI imports. The pinned local toolchain passes
pytest/Hypothesis, strict mypy, and Ruff. See the
[planner documentation](PLANNER.md).

### Slice 2 — read-only adapter and coherent snapshots

- Wrap the official IBKR client behind `ReadOnlyBroker`.
- Use a fixed loopback host and nonzero client ID.
- Correlate callbacks by request and connection epoch; require end callbacks
  where the protocol provides them.
- Retrieve and verify account, position, exact contract details, open-order
  snapshot, quote and market-data type, market rule, server time, and settings.
- Convert callbacks into immutable snapshots. Surface every error as data and
  never substitute a default after a timeout.
- Redact account IDs and contract descriptions in ordinary logs; provide an
  explicit diagnostic mode for local troubleshooting.

Exit: deterministic callback-replay tests plus a read-only paper-TWS smoke
test pass.

Result: complete. The official-API adapter exposes only one bounded `capture`
operation, the coordinator publishes only complete exact-identity snapshots,
failed refreshes discard prior state, and ordinary CLI output redacts account
and contract descriptions. Deterministic capture-replay,
timeout/invalidation, expiry, identity, multiplier, interface, and static
no-write tests pass. The redacted paper-TWS smoke returned `READY` with all
eight completion barriers, safety settings, account identity, exact contract,
quote, market rule, position, and open-order snapshot verified. See the
[snapshot documentation](SNAPSHOTS.md) and
[compatibility evidence](compatibility/TWS_10_50_1e_API_10_45_1.md).

### Slice 3 — thin desktop workflow

Implement one linear screen:

1. Connection evidence: loopback address, port, client ID, server/API versions,
   configured/observed paper account, read-only setting, connection epoch, and
   snapshot ages.
2. Eligible positions: only fully verified positive single-leg `OPT`
   positions; ineligible positions remain visible with reasons.
3. Plan request: tranche size, explicit target percentages or start/step/count,
   stop loss percentage, required remainder policy, TIF, and trigger method.
4. Preview: position and allocation arithmetic, unit basis, quote context,
   market rule, raw and rounded prices, every target/stop pair and logical OCA
   group, and ordered validation results.

The window must permanently show `READ-ONLY PREVIEW — ORDERS CANNOT BE SENT`.
There is no arm, submit, confirm, cancel, or modify control.

Exit: GUI tests verify blocked states, refresh/invalidation behavior, keyboard
flow, and that only view-model commands are reachable.

Result: implementation complete; interactive paper-TWS desktop smoke pending.
The Qt-independent view model exposes only refresh and pure preview operations,
and the PySide6 window permanently identifies itself as non-trading. It shows
connection evidence, exact selected-position identity, normalized basis, quote
and market rule, allocation, target/stop pairs, logical OCA groups, price-route
marks, and every validation. GUI tests cover blocked state replacement,
connection-field invalidation, repeated preview, keyboard flow, and the absence
of order-action controls. For the quick-hack workflow, selection is an explicit
conId rather than a browsable all-position inventory; see
[desktop preview documentation](DESKTOP_PREVIEW.md).

### Slice 4 — adversarial verification and handoff

- Run the complete unit, property, replay, GUI, and paper integration suites.
- Exercise reconnects, timeouts, partial fills represented in snapshots,
  rejected read requests, stale quotes, duplicate preview clicks, application
  restarts, and TWS-side manual order changes.
- Compare contract identity, basis normalization, allocation, and prices with
  the paper TWS UI for documented fixtures.
- Document install steps for the pinned official API, TWS settings, the test
  command, known paper/live differences, and the evidence that no trading
  method is reachable.

Exit: all acceptance criteria below are evidenced in the repository.

## Verification matrix

The initial local command should be:

```sh
python -m pytest
```

Add separate markers so the default suite never requires TWS:

```sh
python -m pytest -m paper_tws
```

Required coverage:

| Risk | Required tests |
| --- | --- |
| Quantities | zero/negative/fractional positions rejected; tranche sizes 1 through position size; all remainder policies; allocated plus planned never exceeds position. |
| OCA shape | exactly two closing sells per logical group; equal target/stop quantity; unique stable logical group per tranche; intended OCA type 2 shown. |
| Rounding | every market-rule band edge; exact ticks; repeating decimals; target and stop directional rounding; round crossing into a new band; invalid/zero increment rejected. |
| Duplicate action | repeated planning produces the same fingerprint; repeated GUI preview does not send broker messages; a static production-code check rejects trading method names. |
| Partial fills | position reduction and order `remaining` changes recompute allocation; inconsistent status/remaining data blocks. |
| Rejections/errors | request-scoped and connection-scoped IBKR errors appear as blocking validations; no cached fallback is used. |
| Reconnects | connection epoch changes invalidate all snapshots and plans; no plan is valid until every required feed completes again. |
| Restarts | no prior broker state or valid plan is restored; a complete fresh snapshot is required. |
| Manual TWS changes | adding, editing, partially filling, or cancelling a manual paper order changes the order fingerprint, allocation, and plan validity after refresh. |
| Contract identity | mismatches in account, conId, `OPT`, expiry, strike, right, multiplier, currency, or trading class block. Multiple contract-detail matches block. |
| Quotes | live/frozen/delayed/delayed-frozen labeled; missing, crossed, nonpositive, or stale values cannot be presented as current. |
| Safety surface | production broker interface exposes no write method; network target is loopback; account allowlist mismatch and unknown read-only state block. |

Property tests should assert for every successful `PlanResult`:

```text
0 < planned_quantity <= position_quantity - allocated_quantity
sum(pair.quantity) == planned_quantity
pair.target.quantity == pair.stop.quantity
all(intent.action == SELL)
all(intent.con_id == selected.con_id)
all(intent.account == selected.account)
all(price is valid under the selected market rule)
```

## Acceptance criteria

- No production source calls or exposes `placeOrder`, `cancelOrder`,
  `reqGlobalCancel`, `exerciseOptions`, `reqOpenOrders`, or
  `reqAutoOpenOrders`.
- The application cannot connect to a non-loopback address.
- Unknown paper-account identity or unknown/false read-only setting blocks a
  valid preview.
- Only positive, integral, fully verified `OPT` positions are selectable.
- A snapshot is not published until all required completion barriers arrive;
  any timeout or relevant error is visible and blocking.
- Allocation includes all visible closing exposure without double-counting a
  recognized target/stop OCA pair. Ambiguity blocks.
- The plan is pure and deterministic; values use decimal arithmetic and all
  rounding decisions are displayed.
- Every target has equal stop coverage in its own logical OCA group and every
  order intent is a closing `SELL`.
- Remainder policy, TIF, stop trigger method, quote type, snapshot age, and
  execution-risk warnings are always visible.
- The GUI has no order-action control, and tests prove that previews emit no
  broker write messages.
- The documented unit command and the explicitly selected paper-TWS command
  pass on the pinned versions.
- Paper tests are described as necessary simulation evidence only, never as
  evidence of live stop or complex-order behavior.

## Explicitly out of scope

- Any live-account connection or live order transmission.
- `placeOrder`, including `whatIf` orders, because it still crosses the order
  interface and is unnecessary for this milestone.
- Order modification, cancellation, exercise, or global cancel.
- Arming, confirmation, submission sequencing, acknowledgements, recovery,
  persistence, ownership tags, or idempotency records.
- Importing or managing multi-leg, short, stock, futures-option, or fractional
  positions.
- Treating delayed or missing market data as live.
- Bypassing TWS order precautions.

## Primary references checked

- [TWS/API configuration and read-only behavior](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/tws-configuration-for-api-use/introduction)
- [Active orders for the current API client and read-only rejection](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/api-clients-orders)
- [One-shot `reqAllOpenOrders` behavior](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/all-submitted-orders)
- [Position subscription behavior](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/positions/request-positions)
- [Market-rule lookup](https://www.interactivebrokers.com/docs/tws-api/doc/orders/minimum-price-increment/request-market-rule)
- [Paper-trading limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading)
- [Official TWS API installation source](https://www.interactivebrokers.com/docs/tws-api/doc/download-the-tws-api/introduction)
- [TWS API changelog](https://www.interactivebrokers.com/docs/tws-api/changelog)
