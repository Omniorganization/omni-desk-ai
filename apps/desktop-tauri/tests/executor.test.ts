import assert from 'node:assert/strict';
import test from 'node:test';
import { advertisedRuntimeCapabilities, executeRuntimeTask } from '../src/executor';

test('advertises only executable runtime capabilities', () => {
  assert.deepEqual(advertisedRuntimeCapabilities(), [
    'chat',
    'local-runtime',
    'shell_sandbox',
    'file_operation',
    'dry_run',
  ]);
});

test('unknown capability fails closed instead of falling back to dry-run', async () => {
  const result = await executeRuntimeTask({
    task_id: 'task-unsupported',
    title: 'Unsupported capability',
    capability: 'browser_automation' as never,
  });
  assert.equal(result.status, 'failed');
  assert.match(result.summary, /unsupported runtime capability/);
});

test('file operations require approval scope before native invocation', async () => {
  const result = await executeRuntimeTask({
    task_id: 'task-write',
    title: 'Write file',
    capability: 'file_operation',
    scope: { operation: 'write_file' },
    filesystem_policy: 'workspace_only',
    network_policy: 'none',
    artifact_policy: 'summary',
  });
  assert.equal(result.status, 'failed');
  assert.match(result.summary, /approval_id/);
});

test('task without explicit capability remains a bounded dry-run', async () => {
  const result = await executeRuntimeTask({
    task_id: 'task-dry-run',
    title: 'Inspect without side effects',
  });
  assert.equal(result.status, 'completed');
  assert.match(result.summary, /dry-run accepted/);
});
