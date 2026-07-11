import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';
import { OmniApiClient } from '../src/api';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('JSON gateway 5xx details are never exposed to the desktop UI', async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: 'internal stack trace and provider secret' }),
    { status: 502, headers: { 'content-type': 'application/json' } },
  );

  const client = new OmniApiClient({
    baseUrl: 'https://gateway.example.test',
    token: 'operator-token',
    actor: 'desktop-operator',
  });

  await assert.rejects(
    () => client.bootstrap(),
    (error: Error) => {
      assert.equal(error.message, '502 gateway_unavailable');
      assert.doesNotMatch(error.message, /stack trace|provider secret/);
      return true;
    },
  );
});

test('workspace task summaries omit both contents and relative paths', () => {
  const executor = readFileSync(join(process.cwd(), 'src', 'executor.ts'), 'utf8');
  assert.match(executor, /contents and path omitted from status/);
  assert.match(executor, /names and path omitted from status/);
  assert.doesNotMatch(executor, /summary: `workspace read completed: \$\{relativePath\}/);
  assert.doesNotMatch(executor, /summary: `workspace list completed: \$\{relativePath\}/);
});

test('native workspace boundary rejects symlinked approved roots', () => {
  const native = readFileSync(join(process.cwd(), 'src-tauri', 'src', 'main.rs'), 'utf8');
  assert.match(native, /approved workspace root cannot be a symlink/);
  assert.match(native, /workspace root cannot be a symlink/);
  assert.match(native, /symlink_metadata\(&declared\)/);
});
