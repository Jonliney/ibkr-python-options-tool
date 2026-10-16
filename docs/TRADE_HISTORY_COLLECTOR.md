# Trade history collector

The collector supplies durable inputs for [trade analysis](TRADE_JOURNAL.md). It is a
separate read-only process; opening the desktop app does **not** start it.
The desktop dashboard and position Analysis tab read this same database. The
desktop also records verified position observations and selected-contract fills;
minute-price backfilling still requires this collector process.

## Run

Use the existing Python environment and official TWS API installation. Leave TWS
or IB Gateway running, signed into the expected paper account, with the required
option market-data permissions. API read-only mode can remain enabled.

```sh
.venv/bin/ibkr-options-manager collect --account DU1234567
```

The command discovers long option holdings and option executions visible through
TWS, and repeats after 60 seconds between passes. It uses loopback, paper port
7497 and a separate nonzero client ID, 19. Override `--port` or `--client-id` when
needed; do not share an ID with another running process. Requests are sequential
and bounded by `--timeout` (default 30 seconds per phase). Large portfolios and
backlogs can take longer than one minute to process; one-minute bar resolution
does not promise one-minute polling latency. TWS throttling/errors are recorded
and retried on later passes, without aggressive immediate retries.

For a single pass:

```sh
.venv/bin/ibkr-options-manager collect --account DU1234567 --once \
  --database /private/tmp/option-history.sqlite3
```

If execution timestamps do not include a timezone, specify the **actual TWS login
timezone**, rather than assuming it matches the computer's clock:

```sh
.venv/bin/ibkr-options-manager collect --account DU1234567 \
  --execution-timezone Europe/London
```

The optional IANA timezone is recorded alongside normalized execution timestamps.
Explicit zones in broker timestamps take precedence. Unknown formats, missing
zones and ambiguous/nonexistent DST times retain their raw fill evidence without
an invented UTC timestamp. A changed timezone cannot rewrite a previously stored
UTC execution time.

Default storage on macOS is:

`~/Library/Application Support/IBKR Options Manager/trade-history.sqlite3`

On other systems the database is beside the existing paper execution journal in
the user's state directory. It is separate from the order-submission journal and
contains private account IDs and execution details. `--database` selects another
location. Back up the SQLite database while the collector is stopped.

The command prints a compact JSON result for each pass. `COLLECTED` means requests
were processed, **not** that every trading minute or the full trade lifetime is
present. `PARTIAL` means some requests/discoveries failed; `BLOCKED` means the pass
could not proceed. `--once` returns 2 on errors. Continuous operation retries after
`--interval` seconds (minimum 60); Ctrl-C stops it. SQLite errors are surfaced too.

## Saved data and safety assumptions

- `bars`: UTC minute-start timestamp, bid high, low and close, contract ID and
  explicit `BID` basis. No open, volume or tick stream is retained. Prices are
  decimal text, including a valid zero bid. The close supports a later ballpark
  average; high/low retain brief excursions. Each completed minute is unique per
  contract/basis, shared across entries/accounts. Vendor revisions replace that
  minute rather than creating duplicates. The unfinished current minute is
  excluded. A minute's high and low have no recoverable ordering within that bar.
- `contracts`: exact account/contract identity, first observation, request cursor
  and latest request state. Account membership is verified on every connection.
  The adapter resolves conId and verifies security type, expiry, strike, right,
  multiplier, currency and trading class before requesting prices. Missing or
  conflicting identities cannot relabel existing history. Position callbacks can
  omit exchange; SMART is used to resolve that exact conId.
- `fills`: raw execution ID, side, quantity, price, account, contract, broker time,
  permanent ID and currency. Duplicate callbacks are idempotent; conflicting
  evidence for the same execution ID is rejected. Execution corrections with new
  IDs are retained as raw evidence. Future analysis must resolve correction
  revisions before aggregating fills; rows are not automatically separate trades.
- `execution_times`: UTC timestamps and source zones only when unambiguous.
- `position_observations`: quantity and raw broker average cost when a long
  holding is first seen or those values change. This cost is **not** an entry
  timestamp or a reconstructed buy fill. Manual TWS changes are observations,
  never instructions to trade.
- `requests` and `discovery_errors`: acknowledged intervals, returned bar counts,
  empty responses and failures. A timeout, error, invalid bar or disconnect cannot
  publish a partially received history batch or advance its cursor. One bad
  contract does not overwrite another contract's saved prices.

On first discovery, the collector starts with a 24-hour lookback. An older buy
fill with a known UTC timestamp extends the backfill to that entry. Existing
positions may predate TWS's available execution history: those entries remain
unknown, and the initial lookback does not certify full post-entry coverage.
TWS execution retrieval is bounded by its Trade Log configuration (normally the
current day, with supported TWS configuration allowing a longer window). This is
not an account statement import.

Each later pass overlaps the saved cursor by one minute and requests up to one
calendar day at a time. After a longer outage it additionally requests the latest
day, so an old gap cannot prevent saving the latest/final trading session. Older
backfill progresses on following passes; failed intervals remain pending. Empty
acknowledged responses advance the request cursor but are retained as `empty`,
never converted into zero-valued bars or certified as complete coverage. Gaps in
otherwise returned data are also not filled synthetically. The collector does
not infer market calendars or distinguish a missing quote from a closed market.

Once registered, contracts remain tracked even after the position is sold. The
collector makes best-effort requests through the expiry date and the following
UTC day to accommodate exchange timezone differences, then records
`expired_unverified`. This cutoff is only a bound on requests: **it is not an IBKR
grace period or a guarantee that post-expiry data exists**. Saved data survives
expiration and restarts indefinitely. An expired request may fail earlier.

No final quote is labelled an official settlement price. No exercise outcome,
assignment, fees, FX conversion, hypothetical execution or win/loss result is
inferred. The collector does not place, cancel, modify or bind orders, and cannot
supply an execution-eligible planning snapshot.

IBKR supports option BID historical bars, including their separate bid high, low
and close values. Historical access needs appropriate permissions and can be
throttled. Expired options and option EOD data are documented as unavailable; do
not rely on recovering them 24 hours later:

- [Historical bars and BID values](https://www.interactivebrokers.com/docs/tws-api/doc/md/historical-bars)
- [Historical-data limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html)
- [Execution retrieval](https://interactivebrokers.github.io/tws-api/executions_commissions.html)

## Analysis and UI

Retain separate concepts for actual position/fill evidence and the contract's
counterfactual price path through expiry. The database-backed dashboard groups
long trades from flat to flat, resolving execution corrections. Partial fills,
scale-ins, scale-outs and re-entries are retained. Unknown entry times and
incomplete coverage remain visible. Post-sale prices do not change actual
realized P&L. See [journal calculations and limitations](TRADE_JOURNAL.md).

Positions now have Management and Analysis views. The dashboard reports average
observed adverse/favorable excursions and configurable stop-level-before-target
sequences. Entry-minute bars are excluded; scale-in paths start after the final
buy. No order rules are changed by journal thresholds.

## Verification

```sh
.venv/bin/python -m pytest -q tests/test_trade_history.py tests/test_history_broker.py tests/test_cli.py tests/test_source_safety.py
.venv/bin/python -m pytest -q -m 'not gui'
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests
```

Tests use fake TWS callbacks, not a brokerage connection. They exercise account
and contract conflicts, partial fills/corrections, manual quantity changes,
duplicate responses, zero/invalid prices, missing acknowledgements, rejected
requests, disconnects, restarts, long gaps, expiry cutoffs, timestamp ambiguity and
the collector CLI. An actual paper-TWS collection still needs verification of the
account's data entitlement, callback format and final-session coverage. Paper
success would not establish identical live execution behavior.
