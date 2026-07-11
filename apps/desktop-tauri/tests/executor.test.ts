import assert from 'node:assert/strict';
import test from 'node:test';
import { advertisedRuntimeCapabilities, executeRuntimeTask } from '../src/executor';

test('advertises only executable runtime capabilities', () => {
  assert.deepEqual(advertisedRuntimeCapabilities(), ['chat', 'local-runtime', 'shell_sandbox', 'dry_run']);
});

test('unknown capability fails closed instead of falling back to dry-run', async () => {
  const result = await executeRuntimeTask({
    task_id: 'task-unsupported',
    title: 'Unsupported capability',
    capability: 'browser_automation',
  });
  assert.equal(result.status, 'failed');
  assert.match(result.summary, /unsupported runtime capability/);
});

test('task without explicit capability remains a bounded dry-run', async () => {
  const result = await executeRuntimeTask({
    task_id: 'task-dry-run',
    title: 'Inspect without side effects',
  });
  assert.equal(result.status, 'completed');
  assert.match(result.summary, /dry-run accepted/);
});
