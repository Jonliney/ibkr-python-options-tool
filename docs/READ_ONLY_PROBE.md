# Read-only paper-TWS capability probe

This probe implements Slice 0 of the Milestone 1 plan. It can inspect paper
TWS, but it has no interface for placing, modifying, cancelling, or exercising
orders.

## Prerequisites

1. Log in to a **paper** TWS session.
2. In TWS Global Configuration → API → Settings:
   - enable socket clients;
   - enable read-only API;
   - enable localhost-only connections;
   - confirm the paper socket port (normally `7497`).
3. Review and accept the license on the
   [official IBKR API download](https://interactivebrokers.github.io/), then
   install the official Python TWS API matching the installed TWS version. As
   of 2026-09-16, the latest macOS/Unix download is API 10.50 and includes the
   Python API. Do not install the unrelated `ibapi` package from PyPI.
4. Hold at least one positive single-leg option position in the paper account.
5. In paper TWS, manually create a non-transmitted or safely priced option
   order whose permanent ID can be used as the visibility check. The probe will
   only read the order.

For this local repository, the official client and its pinned dependencies are
installed in the ignored `.venv` rather than in system Python. Use
`.venv/bin/python` in the commands below. Recreate that environment from the
official extracted `IBJts/source/pythonclient` package when changing API
versions.

## Run

First, run discovery without a manual-order ID. This run is expected to be
`BLOCKED`, but its redacted JSON output lists `observed_order_perm_ids`:

```sh
.venv/bin/python -m ibkr_options_manager probe \
  --account DU1234567
```

Choose the permanent ID belonging to the manual paper-TWS order, verify it in
TWS, and rerun the probe with that exact ID:

```sh
.venv/bin/python -m ibkr_options_manager probe \
  --account DU1234567 \
  --manual-order-perm-id 9001
```

If the account holds more than one long option, select the intended position
by contract ID on both runs:

```sh
.venv/bin/python -m ibkr_options_manager probe \
  --account DU1234567 \
  --con-id 123456789 \
  --manual-order-perm-id 9001
```

The host is fixed to the literal loopback address `127.0.0.1`. The defaults are
paper TWS port `7497`, nonzero client ID `17`, and a ten-second overall timeout.
The command exits `0` only when every required capability passes. A blocked or
incomplete probe exits `2` and prints each blocker as JSON. Account IDs are
redacted in the output.

The first run cannot pass: the manual order ID is deliberately required for a
passing result. This two-pass flow avoids guessing an ID while proving that an
order created manually in TWS was visible through `reqAllOpenOrders()`.

## Tests

The current probe uses only the Python standard library for its local tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The suite checks fail-closed assessment, loopback and client-ID validation,
account redaction, the narrow broker interface, and a static ban on production
calls to order placement, cancellation, exercise, binding, and global-cancel
methods.

## Current local evidence

On 2026-09-16, the complete probe passed against paper TWS on loopback port
`7497`. It observed server version `223`, both required safety settings, a
completed coherent snapshot, one exact option contract, market data, a market
rule, and a manually entered working paper order through `reqAllOpenOrders()`.
The official Python package identifies itself as API `10.45.1`, while the TWS
launcher identifies itself as `10.50.1e`. The version skew and redacted result
are recorded in the
[compatibility note](compatibility/TWS_10_50_1e_API_10_45_1.md); no full account
identifier or real order permanent ID is stored in the repository.

## Interpretation

A passing paper probe is compatibility evidence for the pinned TWS/API pair.
It is not evidence that live execution, stop triggering, or complex-order
behavior will be identical. Save only redacted output as a project fixture.
