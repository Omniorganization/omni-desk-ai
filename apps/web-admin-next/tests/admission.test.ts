import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { NextRequest } from 'next/server';
import { POST as login } from '../app/api/session/login/route';
import { isGatewaySessionRoute, LOGIN_LIMIT, routedPath, SESSION_LIMIT, SessionAdmission } from '../lib/api-admission';
import { CLIENT_IP_HEADER, clientAddress, clientRateLimitKey, FixedWindowRateLimiter } from '../lib/rate-limit';
import { gatewayRequestHeaders } from '../lib/session';
import { isPlausibleGatewayToken, verifyGatewayIdentity } from '../lib/session-login';
import { ACTOR_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE, SESSION_SEAL_COOKIE } from '../lib/session-names';
import { sealSession, sessionMaxAgeSeconds, verifySessionSeal } from '../lib/session-seal';
import { proxy } from '../proxy';

const SECRET = 'test-fixture-session-secret-not-real-0123456789';
const ENV = { OMNI_WEB_SESSION_SECRET: SECRET };
const SESSION = { token: 'gateway-token', gateway: 'http://127.0.0.1:18789', actor: 'alice', role: 'owner' };
const originalSecret = process.env.OMNI_WEB_SESSION_SECRET;
const originalFetch = globalThis.fetch;

afterEach(() => {
  if (originalSecret === undefined) delete process.env.OMNI_WEB_SESSION_SECRET;
  else process.env.OMNI_WEB_SESSION_SECRET = originalSecret;
  globalThis.fetch = originalFetch;
});

function sessionCookie(session: typeof SESSION, seal: string): string {
  return [[SESSION_COOKIE, session.token], [GATEWAY_COOKIE, session.gateway], [ACTOR_COOKIE, session.actor],
    [ROLE_COOKIE, session.role], [SESSION_SEAL_COOKIE, seal]].map(([name, value]) => `${name}=${encodeURIComponent(value)}`).join('; ');
}

function forwardedFor(address: string): Headers {
  return new Headers({ 'x-forwarded-for': address });
}

test('regression (login_limiter_poc): a few clients cannot lock everyone out of login', () => {
  const limiter = new FixedWindowRateLimiter(LOGIN_LIMIT);
  const t = 1_000_000;
  let admitted = 0;
  for (let ip = 1; ip <= 6; ip += 1) {
    for (let i = 0; i < 10; i += 1) admitted += limiter.allow(`ip:203.0.113.${ip}`, t) ? 1 : 0;
  }
  assert.equal(admitted, 60);
  assert.equal(limiter.allow('ip:203.0.113.1', t + 1), false, 'per-client cap still applies');
  assert.equal(limiter.allow('ip:198.51.100.7', t + 1_000), true, 'an unrelated admin can still log in');
});

test('regression (login_limiter_poc): rejected and distinct-key requests keep memory bounded', () => {
  const limiter = new FixedWindowRateLimiter({ windowMs: 60_000, limit: 1, maxKeys: 1_000 });
  const t = 1_000_000;
  assert.equal(limiter.allow('ip:203.0.113.1', t), true);
  for (let i = 0; i < 10_000; i += 1) assert.equal(limiter.allow('ip:203.0.113.1', t + 1), false);
  assert.equal(limiter.size, 1, 'rejections never create state');
  for (let i = 0; i < 200_000; i += 1) limiter.allow(`k${i}`, t + 2_000);
  assert.ok(limiter.size <= 1_000, `tracked keys stay bounded, got ${limiter.size}`);
  assert.equal(limiter.allow('k199999', t + 2_001), false, 'the newest windows survive eviction');
});

test('fixed window limiter reopens expired windows and evicts expired keys first', () => {
  const limiter = new FixedWindowRateLimiter({ windowMs: 1_000, limit: 2, maxKeys: 2 });
  assert.equal(limiter.allow('a', 0), true);
  assert.equal(limiter.allow('a', 1), true);
  assert.equal(limiter.allow('a', 2), false);
  assert.equal(limiter.allow('b', 500), true);
  assert.equal(limiter.allow('c', 1_200), true, 'expired window of a is swept to make room');
  assert.equal(limiter.allow('b', 1_201), true, 'live window of b survived');
  assert.equal(limiter.allow('b', 1_202), false);
  assert.equal(limiter.allow('a', 1_300), true, 'a starts a fresh window');
});

test('client address is the nearest proxy hop and IPv6 is keyed per /64', () => {
  assert.equal(clientAddress(forwardedFor('203.0.113.7')), '203.0.113.7');
  assert.equal(clientAddress(forwardedFor('198.51.100.77, 203.0.113.7')), '203.0.113.7', 'left entries are client-supplied');
  assert.equal(clientAddress(forwardedFor('203.0.113.7, not-an-ip')), null);
  assert.equal(clientAddress(forwardedFor('203.0.113.7:443')), null);
  assert.equal(clientAddress(forwardedFor('256.1.1.1')), null);
  assert.equal(clientAddress(forwardedFor('fe80::1%eth0')), null);
  assert.equal(clientAddress(new Headers()), null);
  assert.equal(clientAddress(forwardedFor('2001:DB8:0:0:1::1')), '2001:db8::1:0:0:1');
  assert.equal(clientAddress(forwardedFor('::ffff:192.0.2.5')), '192.0.2.5');
  assert.equal(clientRateLimitKey('203.0.113.7'), 'ip:203.0.113.7');
  assert.equal(clientRateLimitKey(null), 'unknown');
  assert.equal(clientRateLimitKey('2001:db8:1:2:aaaa::1'), 'ip6:2001:db8:1:2::/64');
  assert.equal(clientRateLimitKey('2001:db8:1:2:bbbb::2'), clientRateLimitKey('2001:db8:1:2:aaaa::1'));
  assert.notEqual(clientRateLimitKey('2001:db8:1:3::1'), clientRateLimitKey('2001:db8:1:2::1'));
});

test('session seals bind the whole login result, expire, and need a strong secret', async () => {
  const now = 1_800_000_000;
  const seal = await sealSession(SESSION, now, ENV);
  assert.match(seal, /^v1\.1800000000\.[A-Za-z0-9_-]{43}$/);
  assert.equal(await verifySessionSeal(SESSION, seal, now + 10, ENV), true);
  for (const changed of [{ token: 'other-token' }, { gateway: 'https://other-gateway.example' }, { actor: 'mallory' }, { role: 'viewer' }]) {
    assert.equal(await verifySessionSeal({ ...SESSION, ...changed }, seal, now + 10, ENV), false, JSON.stringify(changed));
  }
  assert.equal(await verifySessionSeal({ ...SESSION, gateway: undefined }, seal, now, ENV), false);
  assert.equal(await verifySessionSeal(SESSION, seal, now + 3_601, ENV), false, 'expired');
  assert.equal(await verifySessionSeal(SESSION, seal, now - 61, ENV), false, 'issued in the future');
  assert.equal(await verifySessionSeal(SESSION, seal, now, { OMNI_WEB_SESSION_SECRET: SECRET + 'x' }), false);
  const [, issued, mac] = seal.split('.');
  assert.equal(await verifySessionSeal(SESSION, `v1.0${issued}.${mac}`, now, ENV), false, 'non-canonical issue time');
  // The last of 43 base64url characters carries two unused bits: flipping one keeps the decoded MAC.
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  const malleatedMac = mac.slice(0, -1) + alphabet[alphabet.indexOf(mac.at(-1) as string) ^ 1];
  assert.deepEqual(Buffer.from(malleatedMac, 'base64url'), Buffer.from(mac, 'base64url'));
  assert.equal(await verifySessionSeal(SESSION, `v1.${issued}.${malleatedMac}`, now, ENV), false, 'non-canonical MAC encoding');
  assert.equal(await verifySessionSeal(SESSION, 'junk', now, ENV), false);
  assert.equal(await verifySessionSeal(SESSION, undefined, now, ENV), false);
  assert.equal(await verifySessionSeal({ ...SESSION, token: '' }, seal, now, ENV), false);
  const shortLived = { ...ENV, OMNI_WEB_SESSION_MAX_AGE_SECONDS: '60' };
  assert.equal(await verifySessionSeal(SESSION, seal, now + 61, shortLived), false);
  assert.equal(sessionMaxAgeSeconds({ OMNI_WEB_SESSION_MAX_AGE_SECONDS: 'garbage' }), 3600);
  await assert.rejects(() => sealSession(SESSION, now, { OMNI_WEB_SESSION_SECRET: 'too-short' }), /at least 32/);

  const fallback = await sealSession(SESSION, now, {});
  assert.equal(await verifySessionSeal(SESSION, fallback, now, {}), true, 'per-process key is reused');
  assert.equal(await verifySessionSeal(SESSION, fallback, now, ENV), false);
});

test('regression (gateway_limiter_poc): junk session cookies never reach the gateway', async () => {
  const admission = new SessionAdmission(undefined, ENV);
  const attacker = forwardedFor('203.0.113.66');
  const t = 1_800_000_000_000;
  for (let i = 0; i < 300; i += 1) {
    const path = i % 2 ? '/api/session/current' : '/api/omni/bootstrap';
    const rejection = await admission.admit(path, attacker, { ...SESSION, token: `junk-${i}` }, 'v1.1800000000.' + 'A'.repeat(43), t);
    assert.deepEqual(rejection, { status: 401, error: 'session required' });
  }
  assert.deepEqual(await admission.admit('/api/session/current', attacker, { token: 'junk' }, undefined, t), { status: 401, error: 'session required' });

  const seal = await sealSession(SESSION, Math.floor(t / 1000), ENV);
  assert.equal(await admission.admit('/api/session/current', forwardedFor('198.51.100.7'), SESSION, seal, t), null);
  assert.equal(await admission.admit('/api/session/current', attacker, SESSION, seal, t), null,
    'junk from an address does not use up that address budget');
});

test('sealed sessions are rate limited per client, not globally', async () => {
  const admission = new SessionAdmission(undefined, ENV);
  const t = 1_800_000_000_000;
  const seal = await sealSession(SESSION, Math.floor(t / 1000), ENV);
  const busy = forwardedFor('203.0.113.80');
  for (let i = 0; i < SESSION_LIMIT.limit; i += 1) assert.equal(await admission.admit('/api/omni/projects', busy, SESSION, seal, t), null);
  assert.deepEqual(await admission.admit('/api/session/current', busy, SESSION, seal, t),
    { status: 429, error: 'too many requests', retryAfterSeconds: 60 });
  assert.equal(await admission.admit('/api/omni/projects', forwardedFor('203.0.113.81'), SESSION, seal, t), null);
  assert.equal(await admission.admit('/api/session/current', busy, SESSION, seal, t + SESSION_LIMIT.windowMs), null);
  assert.equal(await admission.admit('/api/session/csrf', busy, {}, undefined, t), null, 'routes that never call the gateway are not admitted here');
  assert.equal(isGatewaySessionRoute('/api/omni/chat/stream'), true);
  assert.equal(isGatewaySessionRoute('/api/session/logout'), false);
  assert.equal(routedPath('/api/session/curren%74'), '/api/session/current');
  assert.equal(routedPath('/api/omni%2Fbootstrap'), '/api/omni/bootstrap');
  assert.equal(routedPath('/api/%E0%A4%A'), null);
});

test('login attempts are limited per client inside the login handler', async () => {
  process.env.OMNI_WEB_SESSION_SECRET = SECRET;
  let gatewayCalls = 0;
  globalThis.fetch = async (_input, init) => {
    gatewayCalls += 1;
    const ok = (init?.headers as Record<string, string>).authorization === 'Bearer test-fixture-good-token';
    return new Response(JSON.stringify({ actor: 'alice', role: 'owner' }), { status: ok ? 200 : 403 });
  };
  const attempt = (address: string, token: string) => login(new Request('https://admin.example/api/session/login', {
    method: 'POST', headers: { 'x-forwarded-for': address, 'content-type': 'application/json' }, body: JSON.stringify({ token }),
  }));
  const statuses = [];
  for (let i = 0; i < LOGIN_LIMIT.limit + 1; i += 1) statuses.push((await attempt('203.0.113.120', 'wrong-token')).status);
  assert.deepEqual(statuses, [...Array(LOGIN_LIMIT.limit).fill(401), 429]);
  assert.equal(gatewayCalls, LOGIN_LIMIT.limit, 'rejected attempts never reach the gateway');
  assert.equal((await attempt('203.0.113.121', 'bad token with spaces')).status, 401);
  assert.equal(gatewayCalls, LOGIN_LIMIT.limit, 'implausible tokens never reach the gateway');

  const ok = await attempt('203.0.113.122', 'test-fixture-good-token');
  assert.equal(ok.status, 200);
  const cookies = Object.fromEntries(ok.headers.getSetCookie().map((raw) => raw.split(';', 1)[0].split('=')).map(([k, v]) => [k, decodeURIComponent(v)]));
  const session = { token: cookies[SESSION_COOKIE], gateway: cookies[GATEWAY_COOKIE], actor: cookies[ACTOR_COOKIE], role: cookies[ROLE_COOKIE] };
  assert.equal(await verifySessionSeal(session, cookies[SESSION_SEAL_COOKIE]), true);
});

test('proxy rejects unsealed sessions and over-budget clients before any route handler', async () => {
  process.env.OMNI_WEB_SESSION_SECRET = SECRET;
  const seal = await sealSession(SESSION, undefined, ENV);
  const request = (cookie: string, address: string, path = '/api/session/current') => new NextRequest(`https://admin.example${path}`, {
    headers: { cookie, 'x-forwarded-for': address },
  });

  for (const path of ['/api/session/current', '/api/session/curren%74', '/api/%6Fmni/bootstrap', '/api/omni%2Fbootstrap', '/%61pi/omni/bootstrap']) {
    const junk = await proxy(request(`${SESSION_COOKIE}=junk`, '203.0.113.90', path));
    assert.equal(junk.status, 401, path);
    assert.deepEqual(await junk.json(), { ok: false, error: 'session required' });
    assert.equal(junk.headers.get('x-middleware-next'), null, 'the route handler never runs');
  }
  assert.equal((await proxy(request('', '203.0.113.90', '/api/%E0%A4%A'))).status, 400);

  const sealedCookie = sessionCookie(SESSION, seal);
  const admitted = await proxy(request(sealedCookie, '203.0.113.91'));
  assert.equal(admitted.headers.get('x-middleware-next'), '1');
  for (let i = 1; i < SESSION_LIMIT.limit; i += 1) await proxy(request(sealedCookie, '203.0.113.91'));
  const limited = await proxy(request(sealedCookie, '203.0.113.91'));
  assert.equal(limited.status, 429);
  assert.equal(limited.headers.get('retry-after'), '60');
  assert.equal((await proxy(request(sealedCookie, '203.0.113.92'))).headers.get('x-middleware-next'), '1');
});

test('gateway identity checks forward the client address and skip implausible tokens', async () => {
  let forwarded: string | undefined;
  await verifyGatewayIdentity('https://gateway.example', 'gateway-token', async (_input, init) => {
    forwarded = (init?.headers as Record<string, string>)[CLIENT_IP_HEADER];
    return new Response(JSON.stringify({ actor: 'alice', role: 'viewer' }), { status: 200 });
  }, '203.0.113.7');
  assert.equal(forwarded, '203.0.113.7');
  assert.equal(isPlausibleGatewayToken('test-fixture-token_~.+/='), true);
  const headers = gatewayRequestHeaders({ [CLIENT_IP_HEADER]: '6.6.6.6', 'idempotency-key': 'k1' }, 'tok', 'alice', 'owner', '203.0.113.7');
  assert.equal(headers.get(CLIENT_IP_HEADER), '203.0.113.7');
  assert.equal(headers.get('idempotency-key'), 'k1');
  assert.equal(headers.get('authorization'), 'Bearer tok');
  assert.equal(gatewayRequestHeaders({ [CLIENT_IP_HEADER]: '6.6.6.6' }, 'tok', 'alice', 'owner', null).get(CLIENT_IP_HEADER), null);
  for (const token of ['', 'has space', 'line\nbreak', 'tökén', 'x'.repeat(1025)]) {
    assert.equal(isPlausibleGatewayToken(token), false, JSON.stringify(token.slice(0, 20)));
  }
});
