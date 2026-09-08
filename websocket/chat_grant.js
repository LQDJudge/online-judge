const crypto = require('crypto');

function verifyChatGrant(token, secret, nowSeconds = Math.floor(Date.now() / 1000)) {
  if (typeof token !== 'string' || !token.includes('.')) return null;
  const separator = token.lastIndexOf('.');
  const encoded = token.slice(0, separator);
  const supplied = token.slice(separator + 1);
  const expected = crypto
    .createHmac('sha256', String(secret))
    .update(encoded)
    .digest('hex');
  if (supplied.length !== expected.length ||
      !crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))) {
    return null;
  }
  try {
    const claims = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
    if (!Array.isArray(claims.channels) || !Array.isArray(claims.room_ids) ||
        !claims.room_ids.every(Number.isInteger) || typeof claims.nonce !== 'string' ||
        claims.nonce.length < 8 || !Number.isInteger(claims.exp) ||
        claims.exp <= nowSeconds || !Number.isInteger(claims.user_id) ||
        !claims.channels.every(channel => typeof channel === 'string')) {
      return null;
    }
    return claims;
  } catch (error) {
    return null;
  }
}

function isChatChannel(channel) {
  return typeof channel === 'string' && channel.length > 16 &&
    channel.slice(16).startsWith('chat_');
}

function chatGrantAllowsChannel(
  claims,
  channel,
  nowSeconds = Math.floor(Date.now() / 1000)
) {
  if (!isChatChannel(channel)) return true;
  return Boolean(
    claims &&
    Number.isInteger(claims.exp) &&
    claims.exp > nowSeconds &&
    Array.isArray(claims.channels) &&
    claims.channels.includes(channel)
  );
}

function parseStartMessage(data) {
  if (!data || typeof data !== 'object' ||
      !Number.isSafeInteger(data.start) || data.start < 0) {
    return null;
  }
  return data.start;
}

function validateChannelFilter(data, maximum) {
  if (!data || typeof data !== 'object' ||
      !Array.isArray(data.filter) || data.filter.length === 0) {
    return { code: 'invalid-filter' };
  }
  if (data.filter.length > maximum) {
    return { code: 'too-many-subscriptions' };
  }
  if (!data.filter.every(channel => (
    typeof channel === 'string' && channel.length > 0 && channel.length <= 100
  ))) {
    return { code: 'invalid-channel' };
  }
  return { filter: data.filter };
}

function isAllowedOrigin(origin, allowedOrigins) {
  // Non-browser backend clients commonly omit Origin and authenticate with the
  // sender secret. Browser connections always send it and must match exactly.
  if (!origin) return true;
  return Array.isArray(allowedOrigins) && allowedOrigins.includes(origin);
}

module.exports = {
  chatGrantAllowsChannel,
  isAllowedOrigin,
  isChatChannel,
  parseStartMessage,
  validateChannelFilter,
  verifyChatGrant
};
