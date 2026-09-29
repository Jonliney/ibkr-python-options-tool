# Amended prices on sold layers

The submission journal starts with the original rounded target and stop prices and their entered percentages. After TWS verifies a price amendment, the app updates only the exact app-owned journal layer identified by both permanent order IDs. It records the requested rounded price and the percentage entered by the user; the execution price remains separate fill evidence and may differ from the limit.

An immediate fill can cancel its OCA sibling and yield IBKR error 202. The app records an amendment after that path only when a fresh, complete execution snapshot verifies the full quantity on the amended permanent order ID. A missing acknowledgement or partial fill does not authorize replacing the journal display values.

Older journal entries do not contain amendment prices or entered percentages. Do not silently infer an old requested limit from its execution price or overwrite its percentage from a later position basis.

For a price-only amendment, a change in the live bid or ask changes only the immediate-sell warning. It does not change the selected OCA identity or quantity. An untouched percentage retains the exact working TWS price even when converting that displayed percentage back to a price would round to another tick. The writer clears IBKR VOL-only callback fields before resending an app-owned LMT or STP; TWS otherwise rejects some valid price amendments with error 321. Broker identity, quantity, status, increments, and acknowledgement checks still fail closed.
