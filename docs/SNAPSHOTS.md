# Read-only coherent snapshots

Slice 2 adds an official-API adapter behind the transport-neutral
`ReadOnlyBroker.capture(request)` seam. It performs one bounded read from paper
TWS and publishes an immutable `BrokerSnapshot` only after all required
completion barriers arrive.

The adapter has no order placement, modification, cancellation, binding,
exercise, or global-cancel path. Its public surface contains only `capture`.

## Paper-TWS smoke test

From the repository root, with paper TWS running and its API configured for
read-only localhost access:

```sh
.venv/bin/python -m ibkr_options_manager snapshot \
  --account YOUR_FULL_PAPER_ACCOUNT_ID \
  --con-id YOUR_EXISTING_LONG_OPTION_CON_ID \
  --timeout 20
```

The default connection is the paper TWS port `7497` on literal loopback
`127.0.0.1`, using nonzero client ID `17`. A successful capture exits zero and
prints `"status": "READY"`. A blocked or incomplete capture exits 2 and prints
its errors as data. Account IDs are redacted in both cases.

Ordinary output omits descriptive option identity and order details. For local
troubleshooting only, add `--diagnostic`; this exposes contract and visible
working-order fields but still redacts the account ID. Avoid sharing diagnostic
output without reviewing it first.

## Publication rules

Every capture must complete server time, managed accounts, positions, all open
orders, TWS safety configuration, exact contract details, quote snapshot, and
market rule. The coordinator also requires exactly one matching position and
contract-detail result and verifies conId, security type, expiry, strike, right,
multiplier, currency, trading class, and local symbol.

Timeouts, callback errors, ambiguous results, identity mismatches, and invalid
multipliers block publication. Starting a refresh invalidates the previous
snapshot first, so failure never falls back to cached broker state. Published
snapshots expire after the configured monotonic TTL. Callback order is
canonicalized so the same observed state produces the same immutable snapshot.

`READY` means the capture is coherent, not that an order can be sent. The pure
planner independently blocks disconnected, stale, non-read-only, non-local,
wrong-account, ineligible-position, malformed-order, quote, and market-rule
states. This milestone contains no transmission path.

## Local verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

Paper behavior is simulation evidence only and does not establish that live
stop or complex-order execution will behave identically.
