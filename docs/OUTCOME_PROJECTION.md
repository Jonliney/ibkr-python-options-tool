# Whole-position outcome projection

The workbench keeps Expected gain and Max loss visible in the action review
column, including while an action is staged. Both are exit-price scenarios,
not promises of execution. The current labels are retained by product decision.

For each fully verified, still-held layer, the target contribution is
`(LMT price - current unit basis) × multiplier × remaining quantity` and the
stop contribution uses the STP price in the same formula. **Expected gain** adds
verified, same-currency realized P&L from sold app-owned layers to the target
sum. **Max loss** uses only the stop-price results for contracts still held;
realized P&L does not offset this open-position risk. Separate displays of
realized P&L and cost basis are deferred to a later task.
Draft layers contribute their proposed prices and quantities. Active layers
contribute observed prices until edited; staged amendments use their proposed
prices. A staged bracket deletion removes its exit scenarios but does not
remove its contracts from the held position.

Whole-position totals require scenarios for every held contract and resolved
broker, fill, and price data. When some contracts have existing orders outside
the app, the projection shows the verified draft and active-layer subtotal
under the Expected gain and Max loss labels, with a sentence identifying how
many held contracts are excluded. A fully acknowledged layer awaiting TWS
verification contributes its submitted target and stop prices as an
illustrative scenario. The status sentence names the pending quantity and
states that those exits may not yet be working in TWS. Pending scenarios do
not count as verified protection or permit another submission.
Unknown sold P&L, uncertain or partial layers, invalid inputs, and market
exits with unknown fill prices remain incomplete.
An overallocated plan is also incomplete.

`POSITION_FULLY_ALLOCATED` blocks creating a new draft because existing orders
already cover the held position. When it is the only blocking validation and
the selected broker snapshot is coherent, it does not block projection of the
verified active layers. Other blocking validations still fail closed.

Parenthesized changes compare a proposed value with the plan loaded from the
current broker snapshot and saved draft rows. For Expected gain, an up arrow
means the projected result increased and a down arrow means it decreased. For
Max loss, an up arrow with an unsigned dollar amount means the projected stop
outcome worsened; a down arrow means it improved. The browser updates this
illustrative preview while editing; the server recomputes it with Decimal
arithmetic when rendering an action. A new snapshot establishes a new
comparison baseline. A market exit cannot have a deterministic P&L before its
fill is observed. The complete-position status sentence is omitted; status
text appears only when a projection needs explanation. Both comparisons use
neutral grey on a dedicated line; an unchanged or unavailable comparison shows
`(—)` to keep the metric height stable.

Verification: `.venv/bin/python -m pytest -q tests/test_outcome.py
tests/test_app_demo.py -k 'not reset_active_prices and not embedded_webview and
not webview'`. Embedded Qt WebEngine tests require a working GUI environment.
