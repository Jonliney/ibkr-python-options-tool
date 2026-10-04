# Discord option alerts: feasibility study

**Status:** Research only; no implementation or trading authorization.  
**Date:** 2026-10-04.  
**Example alert:** `Spy 771p at .87 0dte @everyone`.

## Verdict

An **advisory MVP is feasible** if a Discord bot can be installed in the private server and given access to the selected channel, and if TWS can supply a unique contract and current option quote. The MVP can display the identified contract, live bid/ask, a bounded suggested quantity, and why it accepted or rejected the alert. It must never place an order.

Linking a personal Discord account can identify the user and list their servers, but it **does not grant permission to read arbitrary private channel messages**. Passive listening uses a server-installed bot and its Gateway connection. If the user is only a member and the server administrator will not install a bot with access to that channel, the proposed passive MVP is blocked through Discord's supported API. Automating a normal user account (a “self-bot”) is prohibited. [Discord OAuth2 scopes](https://docs.discord.com/developers/topics/oauth2), [Gateway](https://docs.discord.com/developers/events/gateway), [Discord self-bot policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots).

More precisely: **for a generally available API that automatically delivers every new message from this server channel, yes, a server-installed bot is the route.** A user-installed app can receive the one message that the user explicitly invokes a message command on, without a server bot; Discord's local RPC scopes are an exceptional approved-partner path, not a normal consumer OAuth grant. Neither provides a generally available passive user-account feed. [Application commands](https://docs.discord.com/developers/interactions/application-commands), [OAuth2 scopes](https://docs.discord.com/developers/topics/oauth2).

### If Discord is open on the same Mac

Local access changes the *capture options*, but does not turn the signed-in desktop client into an authorized message API. The following routes are technically different:

| Route | What it could do | Limitation and suitable use |
| --- | --- | --- |
| **User-installed Discord message command** | You right-click the alert and choose an app action such as **Analyze option**. Discord's documented message-command interaction includes the selected message's content, ID, author, channel ID, and edit timestamp. The app need not be installed in the server. | **Promising official one-click route, but user-triggered rather than passive.** The server can restrict `Use External Apps`; availability and the exact interaction payload must be tested in this server. Discord's user-install tutorial uses a public Interactions Endpoint URL, with a tunnel for local development; this is not a direct local desktop hook. [Application commands](https://docs.discord.com/developers/interactions/application-commands), [user-install tutorial](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app), [Using Apps](https://support.discord.com/hc/en-us/articles/21334461140375-Using-Apps-on-Discord). |
| User selects/copies an alert into the app | Parse exact text and resolve/price the contract immediately. A Share/Paste action could make this quick. | Supervised and reliable for Stage 1; no passive listener, no automatic server/channel selection. Best bot-free fallback. |
| macOS Accessibility reads Discord's visible UI | Potentially inspect text exposed in Discord's accessibility tree, with the user's Accessibility permission. | Dependent on Discord's current UI tree, selected channel, scrolling, message rendering, and app updates. Does not supply a durable, complete message feed or authoritative message identity. Feasibility requires a local prototype on the actual client. [Apple accessibility API](https://developer.apple.com/documentation/applicationservices/axuielement). |
| Capture the Discord window and run local OCR | ScreenCaptureKit can capture a chosen window with Screen Recording permission; Apple's Vision framework can recognize text on-device. | Sees only rendered content; OCR can confuse `0/O`, `1/l`, decimals, strike, right, and prices. Misses alerts while the channel is not visible, collapsed, or obscured, and cannot reliably track edits/deletes or message IDs. Appropriate only as an explicitly supervised, confirm-before-use experiment. [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit), [Vision text recognition](https://developer.apple.com/documentation/vision/recognizing-text-in-images). |
| Desktop notifications | Discord lets the user select **All** notifications for a channel; visible previews might be manually captured. | Notifications can be muted, grouped, truncated, hidden, or omitted by focus settings. No dependable channel transcript or order-grade event stream. [Discord notification settings](https://support.discord.com/hc/en-us/articles/215253258-Notifications-Settings-101). |
| Discord local RPC | OAuth lists `messages.read` for the local RPC server, and `rpc`/related scopes are restricted to approved partners. | Not a generally available integration path; we should not assume access for this app. [Discord OAuth2 scopes](https://docs.discord.com/developers/topics/oauth2). |
| Desktop client internals, user token, or network interception | Might expose message traffic, but relies on undocumented client behavior or normal-user automation. | Unsuitable: Discord forbids self-bots and its developer policy prohibits mining/scraping Discord content. We should not build a trading input on this route. [Self-bot policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots), [Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy). |

**Practical recommendation:** with a server bot ruled out, first check whether a **user-installed message command** appears and works in this server. It is a better one-click intake than OCR because Discord hands over the chosen message as structured data, including identity fields. If external apps are disabled or a receiving service is undesirable, use **copy/paste (or user-selected text) into a Stage 1 advisory view**. It can still parse the message, identify the exact option in TWS, track its live quote, and calculate a quantity. A local Accessibility/OCR spike could test whether supervised capture saves clicks, but its output must be shown for confirmation and never drive order submission by itself. This is an engineering/safety recommendation; the cited Discord policy does not explicitly classify every possible personal OCR workflow, so do not read this table as a legal ruling on all screen capture.

For a **passive local advisory watcher**, the most plausible experiment is to keep the alert channel open in Discord, observe its Accessibility tree for newly rendered message text, and use window OCR only when the tree omits content. A short feasibility spike should measure missed alerts, duplicate/reordered reads, author/channel identification, edit/delete detection, latency, and character errors using harmless sample alerts. Repeat with Discord unfocused, minimized, the channel switched, a notification arriving, and after a client update. Success would justify a *best-effort suggestion feed* with an explicit “capture uncertain” state; it would not establish a complete or authoritative feed for Stage 2/3. No such local measurement has been run for this study.

The current product reads held contracts and plans **sell exits**. It does not discover an unheld option from an alert, maintain a live quote for it, or plan a buy-to-open entry. These are new domain capabilities, not a reuse of the existing execution path. The current read-only adapter requests a one-off quote snapshot (`reqMktData(..., snapshot=True)`); tracking price requires a streaming subscription and explicit quote freshness handling. See [`broker/ibkr.py`](../../src/ibkr_options_manager/broker/ibkr.py) and [`PROJECT_BRIEF.md`](../PROJECT_BRIEF.md).

## Access and setup

| User-facing step | Supported mechanism | Limit |
| --- | --- | --- |
| Link Discord identity | OAuth2 authorization code flow with `identify`; optionally `guilds` to show the user's servers. Validate OAuth `state` and store tokens securely. | `guilds` returns basic guild information, not channel message contents. |
| Select server and channel | Intersect the user's candidate server list with servers where the bot is actually installed. Enumerate channels visible to the bot; store immutable guild/channel IDs rather than names. | A server member may lack permission to install a bot. The installer/administrator must grant the bot access to the target channel. |
| Listen for alerts | Bot Gateway `MESSAGE_CREATE` with `GUILD_MESSAGES` and `MESSAGE_CONTENT` intents; channel View Channel permission. Read Message History supports controlled recovery after reconnect. | Message Content is a privileged intent. Current Discord policy allows self-service access for smaller apps, with review thresholds as the app grows; verify the app's actual entitlement before building around it. |
| Handle revisions | Observe edit/delete events and message IDs. | An edit or deletion invalidates a pending suggestion; it must never silently revise a staged order. |

Sources: [Discord OAuth2](https://docs.discord.com/developers/topics/oauth2), [Gateway intents](https://docs.discord.com/developers/events/gateway), [Gateway message events](https://docs.discord.com/developers/events/gateway-events), [permissions](https://docs.discord.com/developers/topics/permissions), [message resource](https://docs.discord.com/developers/resources/message), [Message Content policy](https://support-dev.discord.com/hc/en-us/articles/40281523410967-Changes-to-Privileged-Intent-Access-for-Discord-Apps).

The bot should have only the channel permissions it needs. Reconnect recovery may fetch messages after the last observed ID, subject to Discord rate limits, but old alerts should be displayed as missed and never become actionable. [Get Channel Messages](https://docs.discord.com/developers/resources/message), [rate limits](https://docs.discord.com/developers/topics/rate-limits).

## From message to contract

The example should be treated as a **proposal to resolve**, not a contract identifier:

| Text | Parsed meaning | Required verification |
| --- | --- | --- |
| `Spy` | Underlying symbol `SPY` | Resolve the intended underlying; reject ticker aliases or ambiguous products. |
| `771p` | Strike 771, put | Verify `right=P` and exact strike. |
| `at .87` | Sender's reference premium, $0.87 per share for a standard 100-share contract | Never treat it as a live or executable price. |
| `0dte` | Expiration today in the relevant exchange calendar/time zone | Verify an actually listed expiration and that the option is still tradable. Do not use the computer's local calendar date. |
| `@everyone` | Discord mention, not trade data | Ignore for parsing; sender and channel still require allowlisting. |

The user-configurable “message structure” should begin as a **strict template/preset with a preview**, not arbitrary natural-language trading instructions. Require exactly one symbol, strike, right, expiration expression, and optional reference premium. Reject conflicting prices, multiple tickers/contracts, edited/reposted alerts, unsupported symbols, malformed decimals, and unknown expiry language. An allowlist of sender IDs and a maximum message age are necessary to keep unrelated channel chatter from triggering recommendations.

For IBKR, discover candidate expiries/strikes with `reqSecDefOptParams`, then narrow and confirm with `reqContractDetails`. The option-chain API can return expiry/strike combinations that do not correspond to real contracts, so the final result must be **exactly one** qualified contract. Verify `conId`, underlying, `secType=OPT`, expiration, strike, right, multiplier, currency, trading class, exchange, and selected account before showing an actionable recommendation. If no unique match exists, stop and show the reason. [IBKR option-chain guidance](https://interactivebrokers.github.io/tws-api/options.html), [contract fields](https://ibkrcampus.com/docs/tws-api/ref/contract), [contract details](https://ibkrcampus.com/docs/tws-api/ref/contract-details).

An option alert can be valid syntactically but untradable at receipt: same-day expiry near/after the exchange cutoff, holiday, missing listing, halted market, or unavailable quote. The app should show the failure state rather than infer a nearby expiry or strike. This is a safety inference from the contract and market-data constraints above.

## Stage 1: live price and affordable quantity

Subscribe to the specific qualified option with streaming `reqMktData` and track bid, ask, sizes, quote timestamp/arrival time, market-data type, and connection state. Buying should be sized from a **current ask or a more conservative explicit limit**, not the alert's `.87` or the last trade. The UI should show the alert reference alongside the current quote, spread, price movement, and rejection reason. TWS must have the relevant live market-data subscription; delayed/frozen data are not suitable for a time-sensitive 0DTE recommendation. Missing bid/ask, stale quote, crossed/wide spread, lost connectivity, or delayed data must disable the quantity. [IBKR market-data types](https://interactivebrokers.github.io/tws-api/market_data_type.html), [receiving market data](https://interactivebrokers.github.io/tws-api/md_receive.html), [market-data subscriptions](https://ibkrcampus.com/docs/general/market-data-subscriptions/introduction).

The deterministic sizing rule should be specified as:

`quantity = floor(available_alert_budget / (limit_price × verified_multiplier + conservative_per_contract_fee_reserve))`

Then cap by any independent per-alert, daily, per-symbol, and open-position limits. Round the prospective limit to the contract's applicable market rule, not a guessed penny increment. Recheck the quote, buying power, account, and budget at action time. For a $500 budget, a $1.10 ask and a verified 100 multiplier give at most 4 contracts **before fees**; sizing at the alert's $0.87 would misleadingly suggest 5. Premium is quoted per share for a standard 100-share option, but adjusted contracts exist, so the verified IBKR multiplier governs. [OIC options overview](https://www.optionseducation.org/getattachment/8d382efb-64ba-431f-9b87-b7fc9b0916bf/OIC-Options-Overview-For-Investors-final.pdf%3Flang%3Den-US), [IBKR market rules](https://interactivebrokers.github.io/tws-api/minimum_increment.html).

**Acceptance gate:** In paper TWS, prove exact-contract resolution and deterministic, reproducible sizing across valid alerts, adjusted multipliers, quote changes, stale/delayed data, missing ask, account mismatch, reconnect, duplicate/edit/delete, and insufficient budget. Remain read-only.

## Stage 2: prepare an order for manual transmit

Technically, the TWS API has an order `transmit=false` setting that can create an untransmitted order in TWS. This still calls `placeOrder`, requires API write access, and is therefore a distinct milestone requiring explicit authorization and paper validation. Untransmitted orders are session-local and can disappear when TWS restarts. Whether the exact intended TWS UI workflow permits the user to inspect and manually transmit an API-created order must be tested with the chosen TWS/IB Gateway version and account setup before promising this feature. [IBKR order submission](https://interactivebrokers.github.io/tws-api/order_submission.html), [order fields](https://ibkrcampus.com/docs/tws-api/ref/order).

The proposed user flow is: select an observed suggestion, refresh the contract/quote/account/budget, show a complete order preview, explicitly request staging, confirm that TWS accepted the nontransmitted order and reconcile its ID/status, then let the user inspect and transmit it inside TWS. The app must not assume that a `placeOrder` call itself means an acknowledged, visible, or durable order. It must never modify/cancel an order it does not own. Paper tests should cover restart, disconnect, rejection, duplicate clicks, manual TWS changes, and partial fills. The current sell-exit execution code is not an entry-order implementation.

**Bracket caveat:** IBKR's documented bracket sample uses `transmit=true` on the last child, which transmits the whole bracket; that pattern cannot simply be copied into a manual-transmit stage. A paper-verified nontransmitted parent/children workflow, including what the user actually sees and transmits, would be required. [IBKR bracket orders](https://interactivebrokers.github.io/tws-api/bracket_order.html).

## Stage 3: autonomous entry and brackets

The API can submit a parent buy and attached protective/take-profit orders, but this is a separate high-risk capability and is **not authorized by this study**. Bracket placement is not an atomic guarantee: child submission, acknowledgements, partial fills, rejection, disconnects, and manual changes can leave a position or order state different from the intended plan. A sender's alert does not specify stop and target prices, so bracket rules would need to be explicit and validated independently. [IBKR bracket orders](https://interactivebrokers.github.io/tws-api/bracket_order.html), [order submission and status](https://interactivebrokers.github.io/tws-api/order_submission.html).

Before even considering this stage: build an owned-order journal and idempotency key (`guild/channel/message ID` plus account/contract/plan revision), reconcile open orders and executions after every reconnect/restart, enforce hard dollar/quantity/daily-loss limits, stop on incomplete acknowledgements, and expose a kill switch. Preserve TWS precautions. Test quantities, tick rounding, duplicates, partial fills, rejects, manual TWS changes, reconnects, and restarts in paper trading. Live transmission would require a later explicit user authorization under [`AGENTS.md`](../../AGENTS.md); paper success alone cannot establish live stop/bracket behavior.

Unattended operation also depends on an authenticated, connected TWS or IB Gateway session; IBKR documents login and periodic restart requirements. No design can promise that a Discord alert will execute at the sender's quoted price or without interruption. [IBKR API setup](https://interactivebrokers.github.io/tws-api/initial_setup.html), [market-data types](https://interactivebrokers.github.io/tws-api/market_data_type.html).

## Suggested design boundary

Keep the integration as a chain of small, independently testable decisions:

1. **Discord intake:** authenticated bot events, configured guild/channel/sender, deduplication, age and edit/delete handling. Output an immutable alert envelope.
2. **Pure alert parser:** strict template to a typed option request or a named rejection; no Discord or TWS calls.
3. **Contract resolver:** read-only IBKR lookup to a unique verified `conId` and exact contract attributes, or a named rejection.
4. **Quote observer:** streaming market-data state with freshness and data-type proof; no order writes.
5. **Pure entry planner:** allowance, validated quote, verified multiplier, fees, tick rule, and risk caps to a suggested quantity/limit or a named rejection.
6. **UI:** display source message, exact contract, quote age, budget calculation, and current validity; Step 2 would add an explicit staging action behind a separate broker-write boundary.

The important seam is the **complete alert-to-recommendation state transition**, including invalidation on edits, stale quotes, and reconnects. Splitting only by UI screen would leave the safety rules scattered. This also fits the ongoing observation/reconciliation architecture work.

## Open decisions before implementation

- Can a server administrator install a bot in the private server and grant it access to the target channel? This is the first go/no-go check **for passive monitoring**; manual paste still permits the advisory MVP.
- Which author IDs are trusted, and do messages arrive as plain text, embeds, replies, or edited posts? Obtain several real, redacted examples before fixing the grammar.
- Does “$ allowance” mean per alert, per day, or total premium at risk? What fee reserve, maximum spread/slippage, quote age, and minimum time to expiry are acceptable?
- Which account (paper first), option classes, order session (regular hours only initially), and live market-data entitlements are available?
- For eventual brackets, what exact entry limit, stop, profit target, time exit, and handling of partial fills are intended? No bracket rule can be inferred from the example alert alone.

## Recommendation

For passive monitoring, a server-installed bot remains the supported route; the owner has ruled that out here. Evaluate a user-installed message command for **one-click, user-triggered intake** and use manual paste if the server restricts external apps. Then build **Stage 1 only** as a read-only, paper-connected vertical slice: one strict alert pattern, one qualified option, streaming quote, and a transparent quantity recommendation. Treat Stage 2 and Stage 3 as separately approved milestones with their own paper-trading evidence and safety review.
