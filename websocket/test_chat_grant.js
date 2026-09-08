const assert = require('assert');
const crypto = require('crypto');

const {
  chatGrantAllowsChannel,
  isAllowedOrigin,
  isChatChannel,
  parseStartMessage,
  validateChannelFilter,
  verifyChatGrant
} = require('./chat_grant');

const secret = 'chat-grant-test-secret';
const now = 2000000000;
const claims = {
  channels: ['0123456789abcdefchat_room_42'],
  exp: now + 60,
  nonce: 'random-binding',
  room_ids: [42],
  user_id: 7
};
const encoded = Buffer.from(JSON.stringify(claims)).toString('base64url');
const signature = crypto.createHmac('sha256', secret).update(encoded).digest('hex');
const token = encoded + '.' + signature;

assert.deepStrictEqual(verifyChatGrant(token, secret, now), claims);
assert.strictEqual(verifyChatGrant(token + '0', secret, now), null);
assert.strictEqual(verifyChatGrant(token, 'wrong-secret', now), null);
assert.strictEqual(verifyChatGrant(token, secret, now + 61), null);
assert.strictEqual(isChatChannel('0123456789abcdefchat_room_42'), true);
assert.strictEqual(isChatChannel('ordinary-event-channel'), false);
assert.strictEqual(
  chatGrantAllowsChannel(claims, '0123456789abcdefchat_room_42', now),
  true
);
assert.strictEqual(
  chatGrantAllowsChannel(claims, '0123456789abcdefchat_room_42', now + 61),
  false
);
assert.strictEqual(
  chatGrantAllowsChannel(claims, '0123456789abcdefchat_room_99', now),
  false
);
assert.strictEqual(chatGrantAllowsChannel(null, 'ordinary-event-channel', now), true);
assert.strictEqual(isAllowedOrigin(undefined, ['https://example.com']), true);
assert.strictEqual(
  isAllowedOrigin('https://example.com', ['https://example.com']),
  true
);
assert.strictEqual(
  isAllowedOrigin('https://evil.example', ['https://example.com']),
  false
);

assert.strictEqual(parseStartMessage({ start: 0 }), 0);
assert.strictEqual(parseStartMessage({ start: 42 }), 42);
[null, undefined, {}, { start: -1 }, { start: '1' }, { start: 1.5 }].forEach(
  payload => assert.strictEqual(parseStartMessage(payload), null)
);

assert.deepStrictEqual(
  validateChannelFilter({ filter: ['one', 'two'] }, 2),
  { filter: ['one', 'two'] }
);
assert.strictEqual(validateChannelFilter(null, 2).code, 'invalid-filter');
assert.strictEqual(validateChannelFilter({}, 2).code, 'invalid-filter');
assert.strictEqual(
  validateChannelFilter({ filter: ['one', 'two', 'three'] }, 2).code,
  'too-many-subscriptions'
);
assert.strictEqual(
  validateChannelFilter({ filter: [null] }, 2).code,
  'invalid-channel'
);
assert.strictEqual(
  validateChannelFilter({ filter: ['x'.repeat(101)] }, 2).code,
  'invalid-channel'
);

console.log('Chat grant tests passed.');
