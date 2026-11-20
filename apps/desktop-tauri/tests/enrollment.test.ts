import assert from 'node:assert/strict';
import test from 'node:test';
import type { OmniApiClient } from '../src/api';
import { enrollDesktop, needsEnrollment } from '../src/enrollment';

// A fresh install becomes challenge-verified with an owner-started enrollment;
// without this, a production gateway refuses every signed desktop call.

const identity = { deviceId: 'desk_0123', publicKeyPem: '-----BEGIN PUBLIC KEY-----\nkey\n-----END PUBLIC KEY-----' };
const ENROLLMENT = 'enroll_0123456789abcdef';

function fakeClient(trustLevel = 'challenge_verified') {
  const calls: unknown[][] = [];
  const client = {
    completeEnrollment: async (...args: unknown[]) => { calls.push(['complete', ...args]); return { enrollment: { status: 'paired' } }; },
    enrollmentChallenge: async (...args: unknown[]) => { calls.push(['challenge', ...args]); return { challenge: { challenge_id: 'chal_0123456789abcdef', signing_message: 'omnidesk-device-challenge:v2:chal_0123456789abcdef:hmac-sha256:device_challenge_nonce:00' } }; },
    verifyEnrollment: async (...args: unknown[]) => { calls.push(['verify', ...args]); return { credential: { trust_level: trustLevel } }; },
  } as unknown as OmniApiClient;
  return { client, calls };
}

test('binds the key with the pairing code, then signs the gateway challenge', async () => {
  const { client, calls } = fakeClient();
  const signed: string[] = [];
  const outcome = await enrollDesktop({ client, identity, enrollmentId: ` ${ENROLLMENT} `, pairingCode: 'pairing-code-123', signChallenge: async message => { signed.push(message); return 'base64:sig'; } });
  assert.deepEqual(outcome, { deviceId: 'desk_0123', trustLevel: 'challenge_verified' });
  assert.deepEqual(calls, [
    ['complete', ENROLLMENT, 'pairing-code-123', 'desk_0123', identity.publicKeyPem],
    ['challenge', ENROLLMENT, 'desk_0123'],
    ['verify', ENROLLMENT, 'chal_0123456789abcdef', 'desk_0123', 'base64:sig'],
  ]);
  assert.deepEqual(signed, ['omnidesk-device-challenge:v2:chal_0123456789abcdef:hmac-sha256:device_challenge_nonce:00']);
});

test('refuses malformed input before contacting the gateway', async () => {
  const { client, calls } = fakeClient();
  await assert.rejects(enrollDesktop({ client, identity, enrollmentId: 'enroll-1', pairingCode: 'pairing-code-123', signChallenge: async () => 'x' }), /enrollment id/);
  await assert.rejects(enrollDesktop({ client, identity, enrollmentId: ENROLLMENT, pairingCode: 'short', signChallenge: async () => 'x' }), /pairing code/);
  assert.deepEqual(calls, []);
});

test('a gateway that does not verify the device is an error', async () => {
  const { client } = fakeClient('public_key_registered');
  await assert.rejects(enrollDesktop({ client, identity, enrollmentId: ENROLLMENT, pairingCode: 'pairing-code-123', signChallenge: async () => 'x' }), /did not verify/);
});

test('recognises the refusal that asks for enrollment', () => {
  assert.equal(needsEnrollment(new Error('401 device signature rejected: device_not_challenge_verified')), true);
  assert.equal(needsEnrollment(new Error('401 device signature rejected: signature_mismatch')), false);
  assert.equal(needsEnrollment('device_not_challenge_verified'), false);
});
