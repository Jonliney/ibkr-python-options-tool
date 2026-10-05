#!/usr/bin/env node
// Standalone, user-invoked Discord message-command probe. No TWS integration.
import { writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const API = 'https://discord.com/api/v10';
const COMMAND = 'Inspect option alert';
const appId = process.env.DISCORD_PROBE_APP_ID;
const token = process.env.DISCORD_PROBE_BOT_TOKEN;
const output = process.env.DISCORD_PROBE_OUT ?? join(homedir(), 'Desktop', 'discord-message-probe.json');
const action = process.argv[2];

if (!appId || !token || !['register', 'listen'].includes(action)) {
  console.error('Usage: DISCORD_PROBE_APP_ID=... DISCORD_PROBE_BOT_TOKEN=... node scripts/discord_message_probe.mjs register|listen');
  process.exit(2);
}

async function discordPost(path, body, authenticated = true) {
  const response = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(authenticated ? { Authorization: `Bot ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`Discord HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`);
  }
  return response.status === 204 ? null : response.json();
}

async function register() {
  const command = await discordPost(`/applications/${appId}/commands`, {
    name: COMMAND,
    type: 3, // MESSAGE context menu command.
    integration_types: [1], // USER_INSTALL only.
    contexts: [0], // Invoked in a server channel.
  });
  console.log(`Registered global user-install message command: ${command.name} (${command.id})`);
  console.log('Install the application with "Add to my apps". Do not add it to the server.');
}

async function acknowledge(interaction, message) {
  await discordPost(`/interactions/${interaction.id}/${interaction.token}/callback`, {
    type: 4,
    data: {
      content: message,
      flags: 64, // EPHEMERAL: visible only to the invoking user.
      allowed_mentions: { parse: [] },
    },
  }, false);
}

async function capture(interaction) {
  if (String(interaction.application_id) !== appId ||
      interaction.type !== 2 ||
      interaction.data?.type !== 3 ||
      interaction.data?.name !== COMMAND) {
    return;
  }

  const messageId = interaction.data.target_id;
  const message = interaction.data.resolved?.messages?.[messageId];
  const record = {
    captured_at: new Date().toISOString(),
    guild_id: interaction.guild_id ?? null,
    channel_id: interaction.channel_id ?? null,
    invoked_by_user_id: interaction.member?.user?.id ?? interaction.user?.id ?? null,
    message_id: messageId ?? null,
    message_author_id: message?.author?.id ?? null,
    message_timestamp: message?.timestamp ?? null,
    message_edited_timestamp: message?.edited_timestamp ?? null,
    content: message?.content ?? null,
    embeds: message?.embeds ?? [],
  };

  try {
    writeFileSync(output, `${JSON.stringify(record, null, 2)}\n`, { mode: 0o600 });
    await acknowledge(interaction, message?.content == null
      ? 'Captured metadata, but Discord did not provide message text. See the local probe file.'
      : 'Captured the selected message to the local probe file.');
    console.log(`Captured message ${messageId}; file: ${output}`);
  } catch (error) {
    console.error(`Capture failed: ${error.message}`);
    await acknowledge(interaction, 'Local capture failed. Check the probe terminal.');
  }
}

function listen() {
  const socket = new WebSocket('wss://gateway.discord.gg/?v=10&encoding=json');
  let heartbeat;
  let sequence = null;

  socket.addEventListener('message', (event) => {
    let packet;
    try {
      packet = JSON.parse(event.data);
    } catch {
      return;
    }
    if (packet.s != null) sequence = packet.s;
    if (packet.op === 10) {
      heartbeat = setInterval(() => {
        socket.send(JSON.stringify({ op: 1, d: sequence }));
      }, packet.d.heartbeat_interval);
      socket.send(JSON.stringify({
        op: 2,
        d: {
          token,
          intents: 0, // No passive guild-message subscription.
          properties: { os: 'macos', browser: 'discord_message_probe', device: 'discord_message_probe' },
        },
      }));
    } else if (packet.op === 0 && packet.t === 'READY') {
      console.log(`Connected. Right-click a message > Apps > ${COMMAND}.`);
      console.log(`Selected message will be saved to: ${output}`);
    } else if (packet.op === 0 && packet.t === 'INTERACTION_CREATE') {
      capture(packet.d).catch((error) => console.error(`Interaction failed: ${error.message}`));
    } else if (packet.op === 7 || packet.op === 9) {
      console.error('Discord requested reconnect. Restart the probe.');
      socket.close();
    }
  });
  socket.addEventListener('close', (event) => {
    if (heartbeat) clearInterval(heartbeat);
    console.error(`Discord connection closed (${event.code}). Restart the probe if needed.`);
  });
  socket.addEventListener('error', () => console.error('Discord Gateway connection error.'));
}

try {
  if (action === 'register') await register();
  else listen();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
