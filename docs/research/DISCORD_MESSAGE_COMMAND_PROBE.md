# Test a user-installed Discord message command

This is a **standalone feasibility probe**. It does not connect to IBKR or place orders. It registers one right-click message command, then saves **only a message on which you invoke that command** to `~/Desktop/discord-message-probe.json`. It does not listen to ordinary channel traffic. The local receiver uses Discord's outbound Gateway connection, so no tunnel or public URL is needed. Discord supports Gateway and outgoing-webhook interaction delivery as alternatives. [Discord interaction delivery](https://docs.discord.com/developers/interactions/receiving-and-responding).

## 1. Create a Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application, for example **Option Alert Probe**.
2. In **Installation**, enable **User Install**. Under its default install settings, select the `applications.commands` scope. You do not need to install the app to any server. The sample tutorial also installs its example into a test server because it demonstrates *both* installation modes; skip that part here. [Discord user-install tutorial](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app#installing-your-app).
3. Leave **Interactions Endpoint URL** empty for this Gateway-based test. If the portal has an old endpoint configured, remove it so interactions go to the Gateway. [Discord interaction delivery](https://docs.discord.com/developers/interactions/receiving-and-responding).
4. Copy the **Application ID** from **General Information**. On **Bot**, generate or reset the **Bot Token**. A Discord application has a bot identity for API authentication; this step does **not** add that bot to the private server. Keep the token private and out of source control.

## 2. Register the right-click command

Open Terminal and run the following from the repository. Replace only the application ID; the token prompt hides your typing and keeps the token out of the shell command history.

```zsh
cd /Users/joyo/dev/ibkr-options-manager
export DISCORD_PROBE_APP_ID='YOUR_APPLICATION_ID'
read -s 'DISCORD_PROBE_BOT_TOKEN?Paste bot token: '
echo
export DISCORD_PROBE_BOT_TOKEN
node scripts/discord_message_probe.mjs register
```

This registers a **global MESSAGE command** called **Inspect option alert**, configured for `USER_INSTALL` in a `GUILD` channel. It does not request a server bot installation or a passive message-content intent. [Discord command types and contexts](https://docs.discord.com/developers/interactions/application-commands).

## 3. Install it to your account

In the app's **Installation** page, open its install link and choose **Add to my apps**. Do **not** choose **Add to server**. Confirm it appears under your Discord **User Settings → Authorized Apps**. Discord says user-installed apps can be used in servers the user belongs to; server controls can affect how their responses appear. [Discord user-install tutorial](https://docs.discord.com/developers/tutorials/developing-a-user-installable-app#installing-your-app), [Using Apps](https://support.discord.com/hc/en-us/articles/21334461140375-Using-Apps-on-Discord).

## 4. Start the local receiver and try it

In the **same Terminal window**, run:

```zsh
node scripts/discord_message_probe.mjs listen
```

Wait for **Connected**. In Discord, go to the private target channel, right-click a **harmless test message**, and choose **Apps → Inspect option alert**. The command should respond privately and the file should appear at `~/Desktop/discord-message-probe.json`. Open it locally; do not share the Bot Token. The file should contain the selected message's `content`, message ID, author ID, guild ID, channel ID, and timestamps. It overwrites the previous capture. Stop the receiver with **Ctrl-C** when finished. [Discord message-command payload](https://docs.discord.com/developers/interactions/application-commands#message-commands).

## Interpret the result

| Observation | Meaning / next check |
| --- | --- |
| Command appears and exact text plus IDs reach the Desktop file | **Go:** one-click, user-triggered intake works in this server without installing an app there. |
| Command appears but text is `null` | The command arrived but its payload did not contain message text; inspect the metadata and test a plain-text message. Do not treat it as a viable trading alert input yet. |
| Command absent in target server but present elsewhere | Check the app's User Install setting, global command contexts, authorization, and server restrictions. This is a server-specific no-go if it remains unavailable. |
| Command appears but no file is written, despite **Connected** | The command may not be reaching this Gateway client or the Desktop path may be unwritable. Check the terminal. If necessary, test Discord's alternative outgoing-webhook delivery before concluding the server blocks the command. |
| App responds only to you | Expected for the probe's private (ephemeral) reply; this does not mean the invocation failed. |

**Scope of proof:** Even a successful result handles only messages you personally choose. It does not establish an automatic channel feed, live-price availability, or any order-placement capability.
