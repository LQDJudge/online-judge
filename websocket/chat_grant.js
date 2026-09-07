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

module.exports = { isChatChannel, verifyChatGrant };
