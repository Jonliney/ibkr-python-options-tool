# Future feature: position sizing calculator

Status: Future feature — note only; not implemented in this change.

Add a separate **Calculator** tab for planning a long-option position before entry.
Keep its illustrative calculations independent of order submission.

## Inputs

- Total available capital, with an explicit currency.
- Option contract price, quantity multiplier and verified contract identity.
- Stop loss, initially derived from the current stop settings and editable.
- Take-profit limit(s), initially derived from the current target settings.
- An explicit risk budget per trade, as an amount or percentage of total capital,
  for risk-based sizing. Total capital alone determines affordability; it does
  not determine how much of that capital the user wants to risk.

Use the order-planning stop and target settings for these defaults, rather than
the dashboard's Entry timing analysis thresholds.

## Outputs

- Maximum affordable whole-contract quantity from available capital.
- Suggested quantity within both affordability and the selected risk budget.
- Total entry cost and remaining capital.
- Planned loss at the stop, in money and as a percentage of capital.
- Full-premium loss exposure, separate from planned stop loss: a stop does not
  guarantee its exit price or limit the actual loss to the planned figure.
- Potential gain at each take-profit limit and the planned risk-to-reward ratio.
  With multiple targets, use explicit allocations and show the combined scenario.

For a simple single-target long option, before fees and FX:

- Cost per contract = entry premium × multiplier.
- Planned loss per contract = (entry premium − stop premium) × multiplier.
- Quantity = minimum of floor(capital / cost per contract) and
  floor(risk budget / planned loss per contract).
- Planned loss = quantity × planned loss per contract.
- Potential target gain = quantity × (target premium − entry premium) × multiplier.

Potential gain is a price scenario, not a forecast or promised return. If no risk
budget is supplied, show affordability only and do not imply that deploying all
capital is recommended.

## Later implementation requirements

Handle zero/invalid capital, currency mismatches, invalid price increments,
non-standard multipliers, fees, FX, insufficient capital, and zero/inverted stop
distance. Round quantities down to whole contracts. Identify missing inputs
rather than inventing defaults. Keep all calculations local and deterministic;
no live transmission is authorised by this note. Add tests for quantity limits,
rounding, settings-derived defaults, multiple target allocations and loss cases.
