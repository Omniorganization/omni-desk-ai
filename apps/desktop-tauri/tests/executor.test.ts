import assert from 'node:assert/strict';
import test from 'node:test';
import { actionHash, advertisedRuntimeCapabilities, canonicalJson, executeRuntimeTask, RESULT_TEXT_LIMIT, type Invoke, type RuntimeTask } from '../src/executor';

// A task is reported "completed" only when its approved action really ran.

const readAction = { capability: 'workspace.read_file' as const, scope: { workspace: 'reports', relative_path: 'q3.md' } };

async function approvedTask(action: RuntimeTask['action'] = readAction): Promise<RuntimeTask> {
  return { task_id: 'task-1', title: 'Read the Q3 report', approval_id: 'appr_1', capability: action?.capability, action, action_hash: await actionHash(action) };
}

function recordingInvoke(answers: Record<string, unknown>): { invoke: Invoke; calls: Array<[string, Record<string, unknown>]> } {
  const calls: Array<[string, Record<string, unknown>]> = [];
  const invoke = (async (command: string, args: Record<string, unknown>) => {
    calls.push([command, args]);
    const answer = answers[command];
    if (answer instanceof Error) throw answer;
    return answer;
  }) as Invoke;
  return { invoke, calls };
}

test('advertises exactly the executable workspace capabilities', () => {
  assert.deepEqual(advertisedRuntimeCapabilities(), ['chat', 'local-runtime', 'workspace.read_file', 'workspace.list_directory']);
});

test('the action hash matches the gateway canonical form', async () => {
  // Same bytes as Python json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
  assert.equal(canonicalJson(readAction), '{"capability":"workspace.read_file","scope":{"relative_path":"q3.md","workspace":"reports"}}');
  // The same literal is pinned in tests/test_desktop_executable_tasks.py against the gateway's task_action_hash.
  assert.equal(await actionHash(readAction), 'sha256:9374c2215e21e7adebd8cf20a327ad25707bd1723946b6f08c513646adb5078a');
});

test('reads the approved file and reports its content only in the result', async () => {
  const { invoke, calls } = recordingInvoke({ read_workspace_file: 'quarterly numbers' });
  const outcome = await executeRuntimeTask(await approvedTask(), invoke);
  assert.equal(outcome.status, 'completed');
  assert.deepEqual(calls, [['read_workspace_file', { workspace: 'reports', relativePath: 'q3.md' }]]);
  assert.equal(outcome.result?.content, 'quarterly numbers');
  // The status summary is organization-wide; the path and contents are not in it.
  assert.doesNotMatch(outcome.summary, /q3\.md|quarterly/);
});

test('lists the approved directory', async () => {
  const action = { capability: 'workspace.list_directory' as const, scope: { workspace: '.', relative_path: 'reports' } };
  const { invoke } = recordingInvoke({ list_workspace_directory: ['a.md', 'b.md'] });
  const outcome = await executeRuntimeTask(await approvedTask(action), invoke);
  assert.equal(outcome.status, 'completed');
  assert.deepEqual(outcome.result?.entries, ['a.md', 'b.md']);
});

test('a free-text task without an action fails and runs nothing', async () => {
  const { invoke, calls } = recordingInvoke({});
  const outcome = await executeRuntimeTask({ task_id: 'task-2', title: 'please fix the build', approval_id: 'appr_2' }, invoke);
  assert.equal(outcome.status, 'failed');
  assert.match(outcome.summary, /nothing ran/);
  assert.deepEqual(calls, []);
});

test('an action that differs from the approved one is refused', async () => {
  const task = await approvedTask();
  const tampered = { ...task, action: { ...readAction, scope: { workspace: 'reports', relative_path: 'salaries.csv' } } };
  const { invoke, calls } = recordingInvoke({ read_workspace_file: 'secret' });
  const outcome = await executeRuntimeTask(tampered, invoke);
  assert.equal(outcome.status, 'failed');
  assert.match(outcome.summary, /does not match the one that was approved/);
  assert.deepEqual(calls, []);
});

test('no approval, or an unknown capability, fails closed', async () => {
  const { invoke, calls } = recordingInvoke({});
  const unapproved = { ...(await approvedTask()), approval_id: null };
  assert.equal((await executeRuntimeTask(unapproved, invoke)).status, 'failed');
  const browser = { capability: 'browser_automation', scope: { workspace: '.', relative_path: 'x' } } as unknown as RuntimeTask['action'];
  const outcome = await executeRuntimeTask(await approvedTask(browser), invoke);
  assert.equal(outcome.status, 'failed');
  assert.match(outcome.summary, /unsupported runtime capability/);
  assert.deepEqual(calls, []);
});

test('a failing native command is reported as failed', async () => {
  const { invoke } = recordingInvoke({ read_workspace_file: new Error('path escapes the approved workspace') });
  const outcome = await executeRuntimeTask(await approvedTask(), invoke);
  assert.equal(outcome.status, 'failed');
  assert.match(outcome.summary, /path escapes/);
});

test('large files are truncated to the result limit', async () => {
  const { invoke } = recordingInvoke({ read_workspace_file: 'x'.repeat(RESULT_TEXT_LIMIT + 10) });
  const outcome = await executeRuntimeTask(await approvedTask(), invoke);
  assert.equal(outcome.status, 'completed');
  assert.equal(outcome.result?.truncated, true);
  assert.equal(String(outcome.result?.content).length, RESULT_TEXT_LIMIT);
});
