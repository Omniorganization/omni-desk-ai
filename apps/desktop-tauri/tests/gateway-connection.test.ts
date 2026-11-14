import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { GatewaySettings, GatewayTarget, gatewayDraftState, startGatewayBackgroundLoop } from '../src/gatewayConnection';

const originalFetch = globalThis.fetch;
const CONFIRMED: GatewaySettings = { gatewayUrl: 'https://gateway.example.test', token: 'operator-token', actor: 'desktop-operator' };

beforeEach(() => {
  mock.timers.enable({ apis: ['setInterval'] });
});

afterEach(() => {
  mock.timers.reset();
  globalThis.fetch = originalFetch;
});

function recordFetch(): string[] {
  const urls: string[] = [];
  globalThis.fetch = async (input) => {
    urls.push(input.toString());
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  return urls;
}

async function settle() {
  for (let i = 0; i < 5; i += 1) await new Promise(resolve => setImmediate(resolve));
}

// The calls App.tsx makes on each background tick when the auto worker is off.
async function heartbeatTick(gateway: GatewayTarget) {
  const client = gateway.client(async () => ({ 'x-omnidesk-device-signature': 'base64:sig' }));
  await client.heartbeat('desktop-1', 'online', '1.12.7', ['local-runtime']);
  await client.bootstrap();
}

test('background ticks skip a half-typed gateway URL and only ever reach the confirmed gateway', async () => {
  const urls = recordFetch();
  const errors: unknown[] = [];
  let draft: GatewaySettings = { ...CONFIRMED };
  const stop = startGatewayBackgroundLoop({
    confirmed: GatewayTarget.fromSettings(CONFIRMED),
    draft: () => draft,
    intervalMs: 15000,
    tick: heartbeatTick,
    onError: error => errors.push(error),
  });

  mock.timers.tick(15000);
  await settle();
  assert.deepEqual(urls.map(url => new URL(url).pathname), ['/app/runtime/desktop/heartbeat', '/app/bootstrap']);

  // Every prefix of a new address is some other host; none of them may receive the token or a signed heartbeat.
  for (const gatewayUrl of ['https://gateway.example.t', 'https://gateway.exa', 'https://other.example', 'https://other.example.test/']) {
    draft = { ...CONFIRMED, gatewayUrl };
    mock.timers.tick(15000);
    await settle();
  }
  draft = { ...CONFIRMED, token: 'operator-tok' };
  mock.timers.tick(15000);
  await settle();
  assert.equal(urls.length, 2, `requests sent while the form held unconfirmed changes: ${urls.join(', ')}`);

  // Putting the confirmed address back (a trailing slash is the same gateway) resumes, still only there.
  draft = { ...CONFIRMED, gatewayUrl: 'https://gateway.example.test/' };
  mock.timers.tick(15000);
  await settle();
  stop();
  mock.timers.tick(60000);
  await settle();

  assert.equal(urls.length, 4);
  assert.ok(urls.every(url => new URL(url).origin === 'https://gateway.example.test'), urls.join(', '));
  assert.deepEqual(errors, []);
});

test('keychain values resume only as a saved token with a gateway URL that still validates', () => {
  const incomplete: GatewaySettings[] = [
    { gatewayUrl: '', token: 'operator-token', actor: 'desktop-operator' },
    { gatewayUrl: 'https://gateway.example.test', token: '', actor: 'desktop-operator' },
    { gatewayUrl: 'http://gateway.example.test', token: 'operator-token', actor: 'desktop-operator' },
    { gatewayUrl: 'https://user:pass@gateway.example.test', token: 'operator-token', actor: 'desktop-operator' },
  ];
  for (const saved of incomplete) {
    assert.equal(GatewayTarget.restore(saved), null, JSON.stringify(saved));
  }

  const restored = GatewayTarget.restore({ ...CONFIRMED, gatewayUrl: 'https://gateway.example.test/' });
  assert.ok(restored);
  assert.equal(restored.baseUrl, 'https://gateway.example.test');
  assert.equal(gatewayDraftState(restored, CONFIRMED), 'confirmed');
  assert.equal(GatewayTarget.restore({ ...CONFIRMED, actor: '' })?.actor, '');
});

test('without a confirmed gateway the background loop never sends, even with a token in the form', async () => {
  const urls = recordFetch();
  // What the form holds right after only the token came back from the keychain: the default URL plus the token.
  const draft: GatewaySettings = { ...CONFIRMED, gatewayUrl: 'http://127.0.0.1:18789' };
  const stop = startGatewayBackgroundLoop({ confirmed: null, draft: () => draft, intervalMs: 15000, tick: heartbeatTick, onError: () => {} });
  mock.timers.tick(60000);
  await settle();
  stop();
  assert.deepEqual(urls, []);
});

test('confirming keeps the gateway URL rules', () => {
  for (const gatewayUrl of ['gateway.example.test', 'http://gateway.example.test', 'https://gateway.example.test?x=1', 'https://gateway.example.test#x', 'https://u:p@gateway.example.test']) {
    assert.throws(() => GatewayTarget.fromSettings({ ...CONFIRMED, gatewayUrl }), /Gateway URL must/, gatewayUrl);
  }
  assert.equal(GatewayTarget.fromSettings({ ...CONFIRMED, gatewayUrl: 'http://127.0.0.1:18789/' }).baseUrl, 'http://127.0.0.1:18789');
  // A loopback gateway may allow local admin without a token; connecting stays possible as before.
  assert.equal(GatewayTarget.fromSettings({ ...CONFIRMED, gatewayUrl: 'http://127.0.0.1:18789', token: '' }).token, '');
});

test('any edit to the connection form stays pending until it is confirmed again', () => {
  const confirmed = GatewayTarget.fromSettings(CONFIRMED);
  assert.equal(gatewayDraftState(null, CONFIRMED), 'disconnected');
  assert.equal(gatewayDraftState(confirmed, CONFIRMED), 'confirmed');
  assert.equal(gatewayDraftState(confirmed, { ...CONFIRMED, gatewayUrl: 'https://gateway.example.test/' }), 'confirmed');
  for (const edit of [{ gatewayUrl: 'https://gateway.example' }, { gatewayUrl: 'https://' }, { token: 'other-token' }, { actor: 'someone-else' }]) {
    assert.equal(gatewayDraftState(confirmed, { ...CONFIRMED, ...edit }), 'pending', JSON.stringify(edit));
  }
  assert.ok(confirmed.sameGatewayAs(GatewayTarget.fromSettings({ ...CONFIRMED, token: 'rotated-token' })));
  assert.ok(!confirmed.sameGatewayAs(GatewayTarget.fromSettings({ ...CONFIRMED, gatewayUrl: 'https://other.example.test' })));
  assert.ok(!confirmed.sameGatewayAs(null));
});

test('background tick failures are reported instead of escaping the timer', async () => {
  const errors: unknown[] = [];
  const stop = startGatewayBackgroundLoop({
    confirmed: GatewayTarget.fromSettings(CONFIRMED),
    draft: () => CONFIRMED,
    intervalMs: 5000,
    tick: async () => { throw new Error('401 device_signature_required'); },
    onError: error => errors.push(error),
  });
  mock.timers.tick(5000);
  await settle();
  stop();
  assert.equal(errors.length, 1);
  assert.match(String(errors[0]), /device_signature_required/);
});

test('the desktop app builds API clients only from the confirmed gateway', () => {
  const app = readFileSync(join(process.cwd(), 'src', 'App.tsx'), 'utf8');
  assert.doesNotMatch(app, /new OmniApiClient\(/);
  assert.doesNotMatch(app, /baseUrl:\s*gatewayUrl/);
  assert.doesNotMatch(app, /setInterval\(/);
  assert.match(app, /startGatewayBackgroundLoop\(\{\s*confirmed: confirmedGateway,/);

  // The keychain is written only after the gateway accepted this device's signed registration.
  const connect = app.slice(app.indexOf('async function connect()'), app.indexOf('async function claim()'));
  const validated = connect.indexOf('GatewayTarget.fromSettings(');
  const registered = connect.indexOf('registerDesktop(');
  const persisted = connect.indexOf('persistConfirmedGateway(');
  assert.ok(validated >= 0 && registered > validated && persisted > registered, connect);
  assert.doesNotMatch(connect, /keychainSet\(/);

  // A partial keychain write must not leave a saved gateway next to a different token.
  const persist = app.slice(app.indexOf('async function persistConfirmedGateway('), app.indexOf('function gatewayClient('));
  const writes = [...persist.matchAll(/keychainSet\('([^']+)', ([^)]+)\)/g)].map(match => `${match[1]}=${match[2]}`);
  assert.deepEqual(writes, ["omni.gateway=''", 'omni.operatorToken=gateway.token', 'omni.actor=gateway.actor', 'omni.gateway=gateway.baseUrl']);
});
