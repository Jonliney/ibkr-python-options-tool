#!/usr/bin/env node
// Read-only Discord REST probe. Uses a bot token, never a user account token.
const [channelId, expectedGuildId, suppliedMessageId] = process.argv.slice(2);
const token = process.env.DISCORD_PROBE_BOT_TOKEN;

if (!/^\d+$/.test(channelId ?? '') || !/^\d+$/.test(expectedGuildId ?? '') ||
    (suppliedMessageId && !/^\d+$/.test(suppliedMessageId)) || !token) {
  console.error('Usage: DISCORD_PROBE_BOT_TOKEN=... node scripts/discord_read_channel_message.mjs CHANNEL_ID GUILD_ID [MESSAGE_ID]');
  process.exit(2);
}

async function get(path) {
  const response = await fetch(`https://discord.com/api/v10${path}`, {
    headers: { Authorization: `Bot ${token}` },
  });
  const body = await response.text();
  if (!response.ok) {
    let detail = body;
    try { detail = JSON.stringify(JSON.parse(body)); } catch { /* Keep raw response. */ }
    throw new Error(`GET ${path}: HTTP ${response.status} ${detail.slice(0, 1000)}`);
  }
  return JSON.parse(body);
}

function report(error) {
  console.error(error.message);
  if (/HTTP 403/.test(error.message)) {
    console.error('The bot identity may lack access to this channel. User installation and a message-command interaction do not grant the bot channel-history access.');
  }
  process.exitCode = 1;
}

let channel;
try {
  channel = await get(`/channels/${channelId}`);
  console.log('Channel:');
  console.log(JSON.stringify(channel, null, 2));
  if (channel.guild_id !== expectedGuildId) {
    throw new Error(`Channel belongs to guild ${channel.guild_id ?? '(none)'}, not ${expectedGuildId}`);
  }
} catch (error) {
  report(error);
}

// An explicit ID lets us test the message endpoint even if Get Channel fails.
const messageId = suppliedMessageId ?? channel?.last_message_id;
if (!messageId) {
  report(new Error('No message ID available; pass MESSAGE_ID to test the second endpoint.'));
} else if (!channel || channel.guild_id === expectedGuildId) {
  try {
    const message = await get(`/channels/${channelId}/messages/${messageId}`);
    console.log('\nMessage:');
    console.log(JSON.stringify(message, null, 2));
  } catch (error) {
    report(error);
  }
}
