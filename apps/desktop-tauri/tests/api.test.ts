import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';
import { OmniApiClient } from '../src/api';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

type ContractMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE';
type SharedContractEntry = { role: string; signed: readonly string[] };
type TypedContractCase = {
  readonly surface: 'desktop';
  readonly method: ContractMethod;
  readonly contractPath: string;
  readonly clientPath: string;
  readonly signedInProduction?: boolean;
  readonly invoke: (client: OmniApiClient) => Promise<unknown>;
};

const DESKTOP_TYPED_CLIENT_CONTRACT_CASES: readonly TypedContractCase[] = [
  { surface: 'desktop', method: 'GET', contractPath: '/app/bootstrap', clientPath: '/app/bootstrap', invoke: (client) => client.bootstrap() },
  { surface: 'desktop', method: 'GET', contractPath: '/app/projects', clientPath: '/app/projects', invoke: (client) => client.projects() },
  { surface: 'desktop', method: 'POST', contractPath: '/app/projects', clientPath: '/app/projects', invoke: (client) => client.createProject('Typed desktop project', '', {}, 'desktop-1') },
  { surface: 'desktop', method: 'PATCH', contractPath: '/app/projects/{project_id}', clientPath: '/app/projects/proj_1234567890abcdef', invoke: (client) => client.updateProject('proj_1234567890abcdef', { name: 'Renamed desktop project' }) },
  { surface: 'desktop', method: 'DELETE', contractPath: '/app/projects/{project_id}', clientPath: '/app/projects/proj_1234567890abcdef', invoke: (client) => client.deleteProject('proj_1234567890abcdef') },
  { surface: 'desktop', method: 'POST', contractPath: '/app/devices/register', clientPath: '/app/devices/register', invoke: (client) => client.registerDesktop('desktop-1', 'macOS', ['local-runtime']) },
  { surface: 'desktop', method: 'POST', contractPath: '/app/conversations', clientPath: '/app/conversations', invoke: (client) => client.createConversation('Typed desktop conversation', 'desktop-1') },
  { surface: 'desktop', method: 'GET', contractPath: '/app/conversations/{conversation_id}/messages', clientPath: '/app/conversations/conv-1/messages', invoke: (client) => client.listMessages('conv-1') },
  { surface: 'desktop', method: 'POST', contractPath: '/app/conversations/{conversation_id}/ask', clientPath: '/app/conversations/conv-1/ask', invoke: (client) => client.askConversation('conv-1', 'hello desktop', 'fast', 'desktop-1') },
  { surface: 'desktop', method: 'POST', contractPath: '/app/runtime/desktop/heartbeat', clientPath: '/app/runtime/desktop/heartbeat', signedInProduction: true, invoke: (client) => client.heartbeat('desktop-1', 'online', '1.12.7', ['local-runtime']) },
  { surface: 'desktop', method: 'POST', contractPath: '/app/runtime/desktop/claim', clientPath: '/app/runtime/desktop/claim', signedInProduction: true, invoke: (client) => client.claimTask('desktop-1', ['local-runtime']) },
  { surface: 'desktop', method: 'POST', contractPath: '/app/tasks/{task_id}/status', clientPath: '/app/tasks/task-1/status', signedInProduction: true, invoke: (client) => client.updateTaskStatus('task-1', 'completed', 'done', 'desktop-1') },
  { surface: 'desktop', method: 'GET', contractPath: '/app/sync', clientPath: '/app/sync', invoke: (client) => client.sync(42) },
  { surface: 'desktop', method: 'POST', contractPath: '/app/devices/{device_id}/push-token', clientPath: '/app/devices/desktop-1/push-token', signedInProduction: true, invoke: (client) => client.registerPushToken('desktop-1', 'push-token') },
  { surface: 'desktop', method: 'POST', contractPath: '/app/devices/enrollment/{enrollment_id}/complete', clientPath: '/app/devices/enrollment/enroll-1/complete', invoke: (client) => client.completeEnrollment('enroll-1', 'pair-123', 'desktop-1', 'public-key') },
];

function loadSharedContractIndex(): Map<string, SharedContractEntry> {
  const contractPath = join(process.cwd(), '..', 'shared', 'omni-app-api.contract.json');
  const contract = JSON.parse(readFileSync(contractPath, 'utf8')) as { endpoints: Array<Record<string, unknown>> };
  return new Map(contract.endpoints.map((endpoint) => [
    `${String(endpoint.method)} ${String(endpoint.path)}`,
    {
      role: String(endpoint.role),
      signed: Array.isArray(endpoint.signed_device_required_in_production)
        ? endpoint.signed_device_required_in_production.map(String)
        : [],
    },
  ]));
}

test('desktop typed client contract cases match shared contract and emitted requests', async () => {
  const contract = loadSharedContractIndex();

  for (const contractCase of DESKTOP_TYPED_CLIENT_CONTRACT_CASES) {
    const sharedEntry = contract.get(`${contractCase.method} ${contractCase.contractPath}`);
    assert.ok(sharedEntry, `${contractCase.method} ${contractCase.contractPath} must exist in shared contract`);
    assert.ok(sharedEntry.role.length > 0, `${contractCase.contractPath} must declare a role`);
    if (sharedEntry.signed.includes(contractCase.surface)) {
      assert.equal(contractCase.signedInProduction, true, `${contractCase.contractPath} must be tested as signed in production`);
    }

    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const signedCalls: Array<{ method: string; path: string; body: string }> = [];
    globalThis.fetch = async (input, init) => {
      calls.push({ url: input.toString(), init });
      return new Response(JSON.stringify({ ok: true, device: { device_id: 'desktop-1' }, assistant_message: { content: 'ok' } }), { status: 200 });
    };

    const client = new OmniApiClient({
      baseUrl: 'https://gateway.example.test/',
      token: 'operator-token',
      actor: 'desktop-operator',
      deviceSigner: async (method, path, body) => {
        signedCalls.push({ method, path, body });
        return {
          'x-omnidesk-device-id': 'desktop-1',
          'x-omnidesk-timestamp': '123',
          'x-omnidesk-nonce': 'nonce-1234567890abcdef',
          'x-omnidesk-device-signature': 'base64:sig',
        };
      },
    });

    await contractCase.invoke(client);
    assert.equal(calls.length, 1, `${contractCase.contractPath} should issue one request`);
    const requestUrl = new URL(calls[0].url);
    assert.equal(requestUrl.pathname, contractCase.clientPath);
    assert.equal(String(calls[0].init?.method ?? 'GET').toUpperCase(), contractCase.method);
    assert.equal((calls[0].init?.headers as Record<string, string>).authorization, 'Bearer operator-token');
    assert.equal((calls[0].init?.headers as Record<string, string>)['x-omnidesk-actor'], 'desktop-operator');
    if (contractCase.signedInProduction) {
      assert.deepEqual(signedCalls.map(({ method, path }) => ({ method, path })), [
        { method: contractCase.method, path: contractCase.clientPath },
      ]);
    }
  }
});

test('desktop registration trims the gateway URL and sends operator identity', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, device: { device_id: 'desktop-1' } }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test/', token: 'operator-token', actor: 'desktop-operator' });
  const result = await client.registerDesktop('desktop-1', 'macOS', ['local-runtime']);

  assert.equal(requestUrl, 'https://gateway.example.test/app/devices/register');
  assert.equal(result.device.device_id, 'desktop-1');
  assert.equal((requestInit?.headers as Record<string, string>).authorization, 'Bearer operator-token');
  assert.equal((requestInit?.headers as Record<string, string>)['x-omnidesk-actor'], 'desktop-operator');
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    device_id: 'desktop-1',
    device_type: 'desktop',
    name: 'Omni Desktop Runtime',
    platform: 'macOS',
    capabilities: ['local-runtime'],
  });
});

test('createProject uses the shared /app/projects contract', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, project: { project_id: 'proj_1234567890abcdef', name: 'Desktop Launch' } }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test/', token: 'operator-token', actor: 'desktop-operator' });
  await client.createProject('Desktop Launch', 'Local execution project', {}, 'desktop-1');

  assert.equal(requestUrl, 'https://gateway.example.test/app/projects');
  assert.equal(String(requestInit?.method), 'POST');
  assert.match((requestInit?.headers as Record<string, string>)['idempotency-key'], /^desktop-project-create-14-/);
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    name: 'Desktop Launch',
    description: 'Local execution project',
    metadata: {},
    source_device_id: 'desktop-1',
  });
});

test('heartbeat, task status, and sync use the shared /app contract', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: input.toString(), init });
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'operator-token', actor: 'desktop-operator' });
  await client.heartbeat('desktop-1', 'online', '1.08', ['local-runtime']);
  await client.updateTaskStatus('task-1', 'completed', 'done', 'desktop-1');
  await client.sync(42);

  assert.equal(calls[0].url, 'https://gateway.example.test/app/runtime/desktop/heartbeat');
  assert.equal(calls[1].url, 'https://gateway.example.test/app/tasks/task-1/status');
  assert.equal(calls[2].url, 'https://gateway.example.test/app/sync?since_seq=42');
  assert.equal(JSON.parse(calls[1].init?.body as string).status, 'completed');
});

test('askConversation uses the shared audited chat endpoint', async () => {
  let requestUrl = '';
  let requestInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = input.toString();
    requestInit = init;
    return new Response(JSON.stringify({ ok: true, assistant_message: { content: 'answer' } }), { status: 200 });
  };

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test/', token: 'operator-token', actor: 'desktop-operator' });
  await client.askConversation('conv-1', 'hello desktop', 'fast', 'desktop-1');

  assert.equal(requestUrl, 'https://gateway.example.test/app/conversations/conv-1/ask');
  assert.match((requestInit?.headers as Record<string, string>)['idempotency-key'], /^desktop-ask-conv-1-13-/);
  assert.deepEqual(JSON.parse(requestInit?.body as string), {
    content: 'hello desktop',
    model_profile: 'fast',
    stream: false,
    source_device_id: 'desktop-1',
  });
});

test('gateway errors include the response body', async () => {
  globalThis.fetch = async () => new Response('bad token', { status: 401 });

  const client = new OmniApiClient({ baseUrl: 'https://gateway.example.test', token: 'bad-token', actor: 'desktop-operator' });

  await assert.rejects(() => client.bootstrap(), /401 bad token/);
});
