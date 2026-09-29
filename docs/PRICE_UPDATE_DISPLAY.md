# Amended prices on sold layers

The submission journal starts with the original rounded target and stop prices and their entered percentages. After TWS verifies a price amendment, the app updates only the exact app-owned journal layer identified by both permanent order IDs. It records the requested rounded price and the percentage entered by the user; the execution price remains separate fill evidence and may differ from the limit.

An immediate fill can cancel its OCA sibling and yield IBKR error 202. The app records an amendment after that path only when a fresh, complete execution snapshot verifies the full quantity on the amended permanent order ID. A missing acknowledgement or partial fill does not authorize replacing the journal display values.

Older journal entries do not contain amendment prices or entered percentages. Do not silently infer an old requested limit from its execution price or overwrite its percentage from a later position basis.
