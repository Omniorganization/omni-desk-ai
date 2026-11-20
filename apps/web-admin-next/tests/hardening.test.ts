import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolveGatewayBaseUrl } from '../lib/gateway';
import { sealSession, verifySessionSeal } from '../lib/session-seal';

const session = { token: 'tok', gateway: 'https://gateway.example', actor: 'alice', role: 'owner' };

test('production refuses to seal or accept sessions without a configured secret', async () => {
  // A per-process random key ends every session at each restart and fails across replicas.
  const production = { NODE_ENV: 'production' };
  await assert.rejects(() => sealSession(session, undefined, production), /OMNI_WEB_SESSION_SECRET is required in production/);
  const wellFormed = `v1.${Math.floor(Date.now() / 1000)}.${Buffer.alloc(32).toString('base64url')}`;
  await assert.rejects(() => verifySessionSeal(session, wellFormed, undefined, production), /required in production/);
  const configured = { NODE_ENV: 'production', OMNI_WEB_SESSION_SECRET: 's'.repeat(48) };
  assert.equal(await verifySessionSeal(session, await sealSession(session, undefined, configured), undefined, configured), true);
});

test('production sends the gateway token over https only, loopback excepted', () => {
  const production = (extra: Record<string, string>) => ({ NODE_ENV: 'production', ...extra });
  assert.throws(() => resolveGatewayBaseUrl('', production({ OMNI_GATEWAY_URL: 'http://gateway.internal:18789' })), /https in production/);
  assert.equal(resolveGatewayBaseUrl('', production({ OMNI_GATEWAY_URL: 'http://127.0.0.1:18789' })), 'http://127.0.0.1:18789');
  assert.equal(resolveGatewayBaseUrl('', production({ OMNI_GATEWAY_URL: 'https://gateway.example' })), 'https://gateway.example');
  assert.throws(
    () => resolveGatewayBaseUrl('http://other.example', production({ OMNI_GATEWAY_URL: 'https://gateway.example', OMNI_GATEWAY_URL_ALLOWLIST: 'http://other.example' })),
    /https in production/,
  );
  // Development keeps plain http for a configured gateway.
  assert.equal(resolveGatewayBaseUrl('', { NODE_ENV: 'development', OMNI_GATEWAY_URL: 'http://gateway.internal:18789' }), 'http://gateway.internal:18789');
});
