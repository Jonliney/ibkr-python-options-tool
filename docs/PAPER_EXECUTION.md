# Paper OCA execution

This is an explicit, paper-only milestone. It is enabled only with
`--enable-paper-execution`; the default desktop launch remains read-only.

## Execution contract

Immediately before either confirmation is armed or confirmed, the application
requests a new TWS snapshot. Submission is allowed only when that snapshot
proves all of the following:

- the configured account is a managed `DU` account;
- TWS is connected on loopback and API read-only mode has explicitly reported
  `false`;
- positions, open orders, contract details, quote, and market-rule barriers
  completed inside the freshness window;
- the exact option identity agrees on account, `conId`, security type, expiry,
  strike, right, multiplier, currency, trading class, exchange, and local
  symbol;
- every existing selected-option order is either a complete, journal-proven
  app-owned OCA pair (whose remaining quantity is reserved) or absent; and
- the new snapshot produces the same deterministic plan fingerprint the user
  armed on the first click.

The application transmits new app-owned SELL limit/stop OCA pairs. For each
pair, it submits the limit order with `transmit=False`, followed by the stop
with `transmit=True`, using the pair's unique OCA group and OCA type 2.
TWS may still apply its own order precaution and require the user to click
**Transmit** there. The application must not bypass that independent TWS
safety control.

The initial active-layer management action is deliberately narrow: **Sell now
(MKT)** can exit one app-created, fully reconciled OCA layer. It is available
only after two fresh snapshots prove the selected LMT and its SELL STP peer are
the complete two-leg OCA pair, have the same remaining quantity, were created
by the configured API client, and are proven app-owned by the journal. The
writer cancels exactly those two order IDs, waits for both cancellation
acknowledgements, re-reads this API client's open orders to prove neither leg
remains active, and only then submits one new standalone SELL MKT for the
verified quantity. It never cancels a different layer, uses global-cancel, or
touches manual orders. Any rejection, timeout, or incomplete acknowledgement
fails closed with no retry.

## Acknowledgement and recovery

The app waits for an `openOrder` acknowledgement carrying a nonzero permanent
order ID for every submitted order. The staged MKT experiment is stricter: it
needs cancellation evidence for both selected original order IDs, an
API-client open-order recheck showing neither is active, and an `openOrder`
acknowledgement with a nonzero permanent ID for the new standalone MKT. The
complete outcome is recorded in the local journal at:

- macOS: `~/Library/Application Support/IBKR Options Manager/`
- other platforms: `$XDG_STATE_HOME/IBKR Options Manager/` (or
  `~/.local/state/IBKR Options Manager/`)

The fingerprint is journaled as `PREPARED` before the TWS writer opens. Any
writer failure, timeout, or incomplete acknowledgement becomes
`SUBMISSION_UNKNOWN` and blocks that fingerprint from automatic retry. The
next refresh requests both the configured API client's own open orders
(`reqOpenOrders`) and the all-orders inspection view (`reqAllOpenOrders`). If
the client-bound view proves the exact expected complete OCA pairs with the
application's fingerprinted OCA-group prefix and permanent IDs, the journal
recovers them as `RECONCILED` and exposes them in **Active layers**. The
all-orders view alone is never sufficient for management because IBKR does not
bind those rows and can report API order ID `0`.

If that exact recovery check does not pass, the outcome remains unknown: the
application will not retry, adopt a partial pair, or modify anything. Inspect
TWS and create a deliberately new draft only after resolving the state.

## Required paper validation

Market-closed/deterministic tests verify the plan and submission protocol, but
they do not prove broker behaviour. Before considering a live-account
milestone, test in paper TWS that each limit/stop pair is accepted, grouped as
expected, transmits atomically enough for the intended workflow, reports its
permanent IDs, and reacts correctly to fills/cancellations/reconnects. Also
test that the selected pair is cancelled without affecting other OCA groups,
the post-cancel open-order recheck is clean, and the standalone MKT produces
the expected order/execution callbacks.
