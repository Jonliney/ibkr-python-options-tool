# Stop-first bracket validation

Audit date: 2026-10-13. Scope: new paper OCA exit-pair submission, callback
normalization, uncertainty handling and recovery. This is not a live-execution
certification. The preceding full suite passed 694 tests; 43 focused transport
cases exercise the real writer with a simulated IBKR client. The simulations
do not establish TWS routing, precaution or fill behavior.

## Actual sequence

1. Validate the verified paper account, connection, option identity, quantities,
   valid plan and tick increments. Persist a PREPARED journal attempt before
   calling the transport. A journaled uncertain fingerprint cannot auto-retry.
2. Borrow the configured client connection exclusively within this process,
   pausing its observer. Connect and obtain the next order ID.
3. Read all open orders, positions and executions. Require complete responses,
   the exact baseline position/cost and execution IDs, and unchanged existing
   orders for the selected contract. Advance the ID floor above observed IDs.
4. Send the tranche's SELL STP (or STP LMT), GTC, transmit=True, with its own
   OCA group and OCA type 2. Wait for a matching openOrder acknowledgement,
   positive permanent ID and Submitted/PreSubmitted status.
5. Repeat the fresh broker reads. Every sent leg and pre-existing selected-contract
   order must remain present and unchanged; the position and executions must
   match the baseline. Check callback errors and fills again immediately before
   sending the SELL LMT target, transmit=True, in the same OCA group.
6. Wait for target acceptance and repeat those fresh reads for all sent legs.
   Only then proceed to the next tranche. All stages share one bounded deadline.
   Four tranches require nine fresh verification rounds; six require thirteen.
7. Return IDs only after every pair passes. Receipts retain target/stop ordering
   although stop IDs are allocated and transmitted first. Any exception after
   a send becomes an unknown outcome, stops later sends, and is journaled with
   the phase, OCA group and observed status/IDs. No automatic orphan cleanup,
   retry or recreation occurs.

Normalization is narrow: zero/unset/None are equivalent only for an unused STP
limit price or LMT auxiliary price. Both STP LMT prices remain exact. A requested
outsideRth=True may be acknowledged as False only after code 2109 for that exact
submitted order ID. This does not promise execution outside regular hours.

The failure modal blocks changes for the selected contract. A fresh, matching
healthy pair can release that lock. A terminal outcome additionally requires
no surviving sibling; manually confirming absence alone is insufficient.

## Evidence and remaining limits

Covered by deterministic tests: stop-first ordering; four tranches; positive IDs;
held and rejected stops; rejected targets; missing legs before/after target;
manual price, contract and permanent-ID changes; position changes; fills and
partial fills; cancellation at a send boundary; disconnect; missing completion;
late execution responses; unchanged external reservations; duplicate retry;
journal reload; modal persistence; sibling survival after a fill; closed-position
order evidence and its expiration; code 2109 and field normalization.

Two additional temporary audit probes reproduced these limits without TWS writes:

- A fill injected at entry to target placeOrder, after the last pre-send check,
  still permits that target call. The writer detects the fill, raises unknown
  and stops afterward. Separate reads and submissions are not atomic. The app
  cannot guarantee prevention of an orphan target or undo an executed sell.
- With a persisted SUBMISSION_UNKNOWN entry and neither a current nor a
  session-closed selected contract, the recovery modal is absent. The journal
  still blocks the exact fingerprint. This exposes a recovery visibility gap
  relevant to restarting after a full position close. An account-wide unresolved
  attempt lookup and a permanent restart-with-no-position regression are needed.
  The probe checked the selection branch, not an end-to-end TWS restart.

Further validation gaps:

- Most simulated callbacks execute synchronously. Add queued reader-thread
  tests with deterministic barriers at every send/read boundary, duplicate,
  delayed, omitted and reordered callbacks, and late code 2109. A late warning
  after an already rejected field comparison should currently fail closed.
- Inject process termination before/after each send, acknowledgement and journal
  update. Verify recovery from PREPARED, not merely a clean journal reload after
  an exception. Test the no-held-position/orphan case especially.
- Test slow six-layer runs against the shared deadline. Timeout after some
  accepted pairs must stop later tranches and expose uncertainty, not retry.
- Test partial fills of quantity >1 in real paper TWS, including OCA sibling
  reductions, immediate target execution and system rejection of a sibling.
- Test simultaneous app instances. Client leasing is process-local and journal
  writes use atomic replacement, without cross-process locking or explicit
  fsync. Current tests do not prove concurrent-process or power-loss safety.

## Reproducible automated checks

```sh
.venv/bin/python -m pytest -q tests/test_bracket_transmission.py tests/test_execution.py tests/test_order_observation.py tests/test_snapshot.py tests/test_app_orders.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

For stronger coverage, use a seeded event-sequence harness through the real
transport. Record each seed and event trace. Assert these invariants:

- No target send without a verified accepted stop at the last completed check.
- No further send after a detected fill, cancellation, conflict or disconnect.
- Every successful receipt identifies all expected, verified pairs.
- No modification or cancellation of an external order.
- No automatic duplicate attempt after uncertainty or restart.
- No recovery clearance while a sibling remains working or evidence is stale.

The first invariant deliberately describes the last completed check; it cannot
assert that the stop is still live at the instant the later target reaches TWS.

## Paper-TWS acceptance matrix

Run manually on a verified paper account. Record TWS/API versions, client IDs,
precaution settings, session hours, order/permanent IDs, OCA groups, quantities,
callback timestamps, final open orders, fills and journal state. Change one
variable per run. Resolve any uncertain attempt in TWS before a new run.

| Scenario | Required observation |
| --- | --- |
| 1, 4 and 6 tranches; STP and STP LMT | Each target follows its stop check; all pairs match; no unexpected Transmit holds |
| Mixed quantities, including 2/1/1/1 | Each OCA pair reserves its tranche quantity once; no over-allocation |
| External exits on the same option | External orders unchanged; combined reservations within held quantity |
| Target fills immediately | Matching sibling cancellation/reduction observed; no later sends after detected fill |
| Stop fills during creation | Changed position/fills halt the run; any target sibling stays visible for review |
| Partial fill on either leg | Remaining OCA quantities and position reconcile; creation halts on detected fill |
| Manually cancel/edit a leg during creation | No target after a detected stop conflict; no later tranches after a detected conflict |
| TWS precaution or system rejection | No bypass; any surviving sibling remains unresolved and visible |
| Disconnect or stop app after first STP | Restart recovers uncertainty without duplicate sends |
| Restart after full close with surviving target | Account-wide orphan warning must appear; currently a known recovery gap |
| Cleanup and confirmed absence | Fresh order/fill evidence supports clearance; new attempt has distinct OCA identities |

IBKR describes OCA type 2 as proportional reduction with block/overfill protection:
[Order reference](https://www.interactivebrokers.com/docs/tws-api/ref/order).
That does not establish an atomic guarantee for adding a target after a stop
already fills. IBKR also notes that system rejection/cancellation of one leg
need not cancel its siblings:
[OCA glossary](https://www.interactivebrokers.com/campus/glossary-terms/one-cancels-all-oca-order/).

Paper results are necessary evidence for this feature, but do not certify
identical live stop execution or complex-order behavior.
