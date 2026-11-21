import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CONTRACT_HEADER, CONTRACT_VERSION, ContractMismatchError } from '../lib/generated/contract';
import { gatewayContractFailure, gatewayRequestHeaders } from '../lib/session';
import { verifyGatewayIdentity } from '../lib/session-login';

// Web Admin checks every gateway answer against the contract it was built for
// (apps/shared/omni-app-api.contract.json) before passing it to the browser.

const major = Number(CONTRACT_VERSION.split('.')[0]);
const answer = (body: unknown, version: string | null = CONTRACT_VERSION, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...(version ? { [CONTRACT_HEADER]: version } : {}) } });

test('the proxy announces the contract to the gateway', () => {
  assert.equal(gatewayRequestHeaders({}, 'tok', 'alice', 'owner', null).get(CONTRACT_HEADER), CONTRACT_VERSION);
});

test('a gateway answer that meets the contract passes, whatever it adds', () => {
  const body = JSON.stringify({ ok: true, conversation: { conversation_id: 'conv_1', title: 't', added_later: 1 } });
  assert.equal(gatewayContractFailure('POST', '/app/conversations', answer(null, `${major}.42`), body), null);
});

test('another major, or a missing field the UI reads, is a contract failure', () => {
  assert.match(gatewayContractFailure('GET', '/app/bootstrap', answer(null, `${major + 1}.0`), '{}') || '', /incompatible/);
  assert.match(gatewayContractFailure('POST', '/app/conversations', answer(null), JSON.stringify({ conversation: {} })) || '', /conversation\.conversation_id: missing/);
  // Error answers are passed on as they are; only their version is checked.
  assert.equal(gatewayContractFailure('POST', '/app/conversations', answer(null, CONTRACT_VERSION, 403), '{"detail":"forbidden"}'), null);
});

test('login refuses a gateway of another major and an identity without a role', async () => {
  await assert.rejects(
    () => verifyGatewayIdentity('https://gateway.example', 'gateway-token', async () => answer({ actor: 'alice', role: 'owner' }, `${major + 1}.0`)),
    ContractMismatchError,
  );
  await assert.rejects(
    () => verifyGatewayIdentity('https://gateway.example', 'gateway-token', async () => answer({ actor: 'alice' })),
    /role: missing/,
  );
  let sent: string | undefined;
  await verifyGatewayIdentity('https://gateway.example', 'gateway-token', async (_input, init) => {
    sent = (init?.headers as Record<string, string>)[CONTRACT_HEADER];
    return answer({ actor: 'alice', role: 'owner' });
  });
  assert.equal(sent, CONTRACT_VERSION);
});
