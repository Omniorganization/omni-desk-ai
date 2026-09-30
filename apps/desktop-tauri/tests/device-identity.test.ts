import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { clearMocks, mockIPC } from '@tauri-apps/api/mocks';
import { loadOrCreateDesktopIdentity, signDesktopDeviceRequest } from '../src/deviceIdentity';

const keys = ['omni.deviceId.v2', 'omni.devicePublicKeyPem.v2', 'omni.devicePrivateKeyJwk.v2'];
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');

beforeEach(() => {
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { crypto: globalThis.crypto } });
});

afterEach(() => {
  clearMocks();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

function store(records: Map<string, string>, readFailure?: string, writeFailure?: string) {
  const writes: string[] = [];
  mockIPC(async (command, payload) => {
    const args = payload as { key: string; value?: string };
    if (command === 'secure_get') {
      if (args.key === readFailure) throw new Error('secure-store read denied');
      return records.get(args.key) || '';
    }
    assert.equal(command, 'secure_set');
    if (args.key === writeFailure) throw new Error('secure-store write denied');
    writes.push(args.key);
    records.set(args.key, args.value!);
  });
  return writes;
}

test('fresh and concurrent initialization creates one durable identity; subsequent load reuses it', async () => {
  const records = new Map<string, string>();
  const writes = store(records);
  const [first, concurrent] = await Promise.all([loadOrCreateDesktopIdentity(), loadOrCreateDesktopIdentity()]);
  assert.deepEqual(concurrent, first);
  assert.match(first.deviceId, /^desk_[a-f0-9]{36}$/);
  assert.match(first.publicKeyPem, /^-----BEGIN PUBLIC KEY-----/);
  assert.deepEqual(writes, keys);
  assert.deepEqual(await loadOrCreateDesktopIdentity(), first);
  assert.equal(writes.length, 3);
  const signature = await signDesktopDeviceRequest(first.deviceId, 'POST', '/app/runtime/desktop/claim', '{}');
  assert.match(signature['x-omnidesk-device-signature'], /^base64:/);
  assert.equal(writes.length, 3);
});

for (const key of keys) {
  test(`read failure for ${key} cannot overwrite existing identity`, async () => {
    const records = new Map(keys.map((entry) => [entry, `existing:${entry}`]));
    const original = new Map(records);
    const writes = store(records, key);
    await assert.rejects(loadOrCreateDesktopIdentity(), /secure-store read denied/);
    assert.deepEqual(records, original);
    assert.deepEqual(writes, []);
  });
}

for (let mask = 1; mask < 7; mask += 1) {
  test(`partial identity ${mask} requires recovery and never rotates surviving records`, async () => {
    const records = new Map(keys.filter((_, index) => mask & (1 << index)).map((key) => [key, `existing:${key}`]));
    const original = new Map(records);
    const writes = store(records);
    await assert.rejects(loadOrCreateDesktopIdentity(), /identity is incomplete/);
    assert.deepEqual(records, original);
    assert.deepEqual(writes, []);
  });
}

for (const key of keys.slice(1)) {
  test(`interrupted initialization at ${key} cannot trigger a replacement on retry`, async () => {
    const records = new Map<string, string>();
    store(records, undefined, key);
    await assert.rejects(loadOrCreateDesktopIdentity(), /secure-store write denied/);
    const original = new Map(records);
    const retryWrites = store(records);
    await assert.rejects(loadOrCreateDesktopIdentity(), /identity is incomplete/);
    assert.deepEqual(records, original);
    assert.deepEqual(retryWrites, []);
  });
}

test('signing propagates backend read errors and never writes', async () => {
  const writes = store(new Map(), undefined);
  mockIPC(async () => { throw new Error('secure-store unavailable'); });
  await assert.rejects(signDesktopDeviceRequest('existing-device', 'POST', '/app/runtime/desktop/claim'), /secure-store unavailable/);
  assert.deepEqual(writes, []);
});
