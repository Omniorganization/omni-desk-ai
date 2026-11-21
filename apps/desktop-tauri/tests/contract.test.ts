import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { OmniApiClient } from '../src/api';
import { CONTRACT_HEADER, CONTRACT_VERSION, ContractMismatchError, RESPONSE_REQUIREMENTS, contractProblems, contractRoute } from '../src/generated/contract';
import { exampleBody } from './contractExample';

// The desktop checks every gateway answer against the contract it was built
// for (apps/shared/omni-app-api.contract.json), so a gateway that speaks
// another major version, or drops a field the desktop reads, fails loudly.

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

const major = Number(CONTRACT_VERSION.split('.')[0]);
const client = () => new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'operator-token', actor: 'desktop-operator' });

function answer(body: unknown, version: string | null = CONTRACT_VERSION, status = 200) {
  const sent: Array<Record<string, string>> = [];
  globalThis.fetch = async (_input, init) => {
    sent.push(init?.headers as Record<string, string>);
    return new Response(JSON.stringify(body), { status, headers: version ? { [CONTRACT_HEADER]: version } : {} });
  };
  return sent;
}

test('request paths map to their contract route', () => {
  assert.equal(contractRoute('post', '/app/devices/enrollment/enroll_0123456789abcdef/verify'), 'POST /app/devices/enrollment/{enrollment_id}/verify');
  assert.equal(contractRoute('GET', '/app/sync?since_seq=4'), 'GET /app/sync');
  assert.equal(contractRoute('GET', '/app/unknown'), undefined);
  assert.equal(contractRoute('GET', '/app/projects//'), 'GET /app/projects');
});

test('the requirement check names what is missing or mistyped', () => {
  const bootstrap = RESPONSE_REQUIREMENTS['GET /app/bootstrap'];
  assert.deepEqual(contractProblems(bootstrap, exampleBody('GET /app/bootstrap')), []);
  assert.deepEqual(contractProblems(bootstrap, { pending_approvals: [{ approval_id: 7, action: 'a', risk: 'high' }], notifications: [], runtime_status: [], devices: [] }), ['pending_approvals[0].approval_id: expected string, got integer']);
  assert.ok(contractProblems(bootstrap, { notifications: [], runtime_status: [], devices: [] }).includes('pending_approvals: missing'));
  // A claim may find nothing: a null task meets the contract.
  assert.deepEqual(contractProblems(RESPONSE_REQUIREMENTS['POST /app/runtime/desktop/claim'], { task: null }), []);
});

test('the desktop announces its contract and accepts a newer minor with extra fields', async () => {
  const sent = answer({ ...exampleBody('GET /app/bootstrap'), added_later: true }, `${major}.99`);
  const snapshot = await client().bootstrap();
  assert.equal(sent[0][CONTRACT_HEADER], CONTRACT_VERSION);
  assert.equal(snapshot.added_later, true);
});

test('a gateway of another major is refused, even when it answers 426', async () => {
  answer(exampleBody('GET /app/bootstrap'), `${major + 1}.0`);
  await assert.rejects(() => client().bootstrap(), ContractMismatchError);
  answer({ ok: false, code: 'contract_version_unsupported' }, `${major + 1}.0`, 426);
  await assert.rejects(() => client().bootstrap(), /incompatible with this client/);
});

test('a gateway from before negotiation is accepted while it meets the contract', async () => {
  answer(exampleBody('GET /app/bootstrap'), null);
  assert.ok(await client().bootstrap());
});

test('an answer missing what the desktop reads is refused', async () => {
  answer({ ok: true, conversation: { title: 'no id' } });
  await assert.rejects(() => client().createConversation('t'), /conversation\.conversation_id: missing/);
});
