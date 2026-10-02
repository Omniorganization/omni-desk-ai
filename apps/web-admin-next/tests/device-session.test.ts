import { createHash, webcrypto } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  loadOrCreateWebAdminIdentity, resetWebAdminIdentity,
  signWebAdminChallenge, signWebAdminDeviceRequest,
} from '../lib/device-identity';

test('one in-memory key signs the exact independent device protocol and resets per session', async () => {
  resetWebAdminIdentity();
  const [identity, same] = await Promise.all([loadOrCreateWebAdminIdentity(), loadOrCreateWebAdminIdentity()]);
  assert.equal(identity.deviceId, same.deviceId);
  const publicKey = await webcrypto.subtle.importKey(
    'spki', Buffer.from(identity.publicKeyPem.replace(/-----(BEGIN|END) PUBLIC KEY-----|\s/g, ''), 'base64'),
    { name: 'ECDSA', namedCurve: 'P-256' }, true, ['verify'],
  );
  const body = '{"decision":"approved"}';
  const path = '/app/approvals/fixture/decide';
  const signed = await signWebAdminDeviceRequest('POST', path, body);
  const message = `omnidesk-device-request:v1:POST:${path}:${createHash('sha256').update(body).digest('hex')}:${signed['x-omnidesk-timestamp']}:${signed['x-omnidesk-nonce']}`;
  assert.equal(await webcrypto.subtle.verify(
    { name: 'ECDSA', hash: 'SHA-256' }, publicKey,
    Buffer.from(signed['x-omnidesk-device-signature'].slice('base64:'.length), 'base64'), Buffer.from(message),
  ), true);
  const challenge = await signWebAdminChallenge('independent challenge');
  assert.equal(await webcrypto.subtle.verify(
    { name: 'ECDSA', hash: 'SHA-256' }, publicKey,
    Buffer.from(challenge.slice('base64:'.length), 'base64'), Buffer.from('independent challenge'),
  ), true);
  resetWebAdminIdentity();
  const fresh = await loadOrCreateWebAdminIdentity();
  assert.notEqual(fresh.deviceId, identity.deviceId);
  assert.notEqual(fresh.publicKeyPem, identity.publicKeyPem);
});
