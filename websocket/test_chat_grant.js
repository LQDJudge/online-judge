const assert = require('assert');
const crypto = require('crypto');

const { isChatChannel, verifyChatGrant } = require('./chat_grant');

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

console.log('Chat grant tests passed.');
