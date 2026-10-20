import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { afterEach, beforeEach, test } from 'node:test';
import { clearMocks, mockIPC } from '@tauri-apps/api/mocks';
import { loadOrCreateDesktopIdentity, signDesktopDeviceRequest } from '../src/deviceIdentity';

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const identity = { deviceId: `desk_${'a'.repeat(36)}`, publicKeyPem: '-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----' };

beforeEach(() => {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { crypto: globalThis.crypto } });
});

afterEach(() => {
  clearMocks();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

function backend(handler: (command: string, args: Record<string, unknown>) => unknown) {
  const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
  mockIPC(async (command, payload) => {
    const args = (payload || {}) as Record<string, unknown>;
    calls.push({ command, args });
    return handler(command, args);
  });
  return calls;
}

test('concurrent identity loads share one native call and never touch the keychain directly', async () => {
  const calls = backend((command) => {
    assert.equal(command, 'device_identity');
    return identity;
  });
  const [first, concurrent] = await Promise.all([loadOrCreateDesktopIdentity(), loadOrCreateDesktopIdentity()]);
  assert.deepEqual(first, identity);
  assert.deepEqual(concurrent, identity);
  assert.deepEqual(calls.map((call) => call.command), ['device_identity']);
});

test('requests are signed natively from validated parts; the private key never reaches the webview', async () => {
  const calls = backend((command, args) => {
    assert.equal(command, 'device_sign_request');
    assert.equal(args.method, 'POST');
    assert.equal(args.path, '/app/runtime/desktop/claim');
    assert.equal(args.bodySha256, createHash('sha256').update('{}').digest('hex'));
    assert.match(String(args.timestamp), /^\d+$/);
    assert.match(String(args.nonce), /^[a-f0-9]{48}$/);
    return 'c2lnbmF0dXJl';
  });
  const headers = await signDesktopDeviceRequest(identity.deviceId, 'post', '/app/runtime/desktop/claim', '{}');
  assert.equal(headers['x-omnidesk-device-id'], identity.deviceId);
  assert.equal(headers['x-omnidesk-device-signature'], 'base64:c2lnbmF0dXJl');
  assert.equal(headers['x-omnidesk-nonce'], calls[0].args.nonce);
  assert.equal(headers['x-omnidesk-timestamp'], calls[0].args.timestamp);
  assert.ok(calls.every((call) => !call.command.startsWith('secure_')));
});

test('native identity and signing errors propagate', async () => {
  backend(() => { throw new Error('desktop device identity is incomplete; secure-store recovery is required'); });
  await assert.rejects(loadOrCreateDesktopIdentity(), /identity is incomplete/);
  backend(() => { throw new Error('desktop device private key is not initialized'); });
  await assert.rejects(signDesktopDeviceRequest('existing-device', 'POST', '/app/runtime/desktop/claim'), /not initialized/);
});
