import { invoke as tauriInvoke } from '@tauri-apps/api/core';

// Executes the one scoped action an approved gateway task carries, inside the
// approved workspace (~/OmniDesktopWorkspace), through the Rust commands that
// refuse escapes and symlinks. Nothing here writes files, runs commands or uses
// the network. A task is reported "completed" only after its action ran.

export type RuntimeCapability = 'workspace.read_file' | 'workspace.list_directory';

export interface TaskAction {
  capability: RuntimeCapability;
  scope: { workspace: string; relative_path: string };
}

export interface RuntimeTask {
  task_id: string;
  title: string;
  approval_id?: string | null;
  capability?: string | null;
  action?: Partial<TaskAction> | null;
  action_hash?: string | null;
}

export interface ExecutionResult {
  status: 'completed' | 'failed';
  summary: string;
  result?: Record<string, unknown>;
}

export type Invoke = <T>(command: string, args: Record<string, unknown>) => Promise<T>;

/** Runs one capability's action; the scope has already been matched to its approval. */
export interface RuntimeExecutor {
  capability: RuntimeCapability;
  run(scope: { workspace: string; relativePath: string }, invoke: Invoke): Promise<ExecutionResult>;
}

const CAPABILITIES: RuntimeCapability[] = ['workspace.read_file', 'workspace.list_directory'];
// Well under the gateway's 64 KiB result limit once JSON-encoded.
export const RESULT_TEXT_LIMIT = 48 * 1024;

export function advertisedRuntimeCapabilities(): string[] {
  return ['chat', 'local-runtime', ...CAPABILITIES];
}

/** JSON with sorted keys and no whitespace: the gateway hashes the same form. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value as Record<string, unknown>).sort();
    return `{${entries.map(key => `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export async function actionHash(action: unknown): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalJson(action)));
  return `sha256:${Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')}`;
}

function failed(summary: string): ExecutionResult {
  return { status: 'failed', summary: summary.slice(0, 500) };
}

function truncate(text: string): { text: string; truncated: boolean } {
  const bytes = new TextEncoder().encode(text);
  if (bytes.length <= RESULT_TEXT_LIMIT) return { text, truncated: false };
  return { text: new TextDecoder().decode(bytes.slice(0, RESULT_TEXT_LIMIT)).replace(/�$/, ''), truncated: true };
}

// The summary is visible to the whole organization: contents, names and path
// go only into the result, which only the task's creator can read.
const EXECUTORS: Record<RuntimeCapability, RuntimeExecutor> = {
  'workspace.read_file': {
    capability: 'workspace.read_file',
    async run({ workspace, relativePath }, invoke) {
      const content = await invoke<string>('read_workspace_file', { workspace, relativePath });
      const { text, truncated } = truncate(content);
      return {
        status: 'completed',
        summary: `workspace file read (${content.length} characters${truncated ? ', truncated in the result' : ''}; contents and path omitted from status)`,
        result: { capability: 'workspace.read_file', workspace, relative_path: relativePath, content: text, truncated },
      };
    },
  },
  'workspace.list_directory': {
    capability: 'workspace.list_directory',
    async run({ workspace, relativePath }, invoke) {
      const entries = await invoke<string[]>('list_workspace_directory', { workspace, relativePath });
      return {
        status: 'completed',
        summary: `workspace directory listed (${entries.length} entries; names and path omitted from status)`,
        result: { capability: 'workspace.list_directory', workspace, relative_path: relativePath, entries },
      };
    },
  },
};

/**
 * Run an approved task's action. Fails closed: no approval, no action, an
 * unknown capability, or an action that does not hash to what was approved
 * reports "failed" and runs nothing.
 */
export async function executeRuntimeTask(task: RuntimeTask, invoke: Invoke = tauriInvoke): Promise<ExecutionResult> {
  if (!task.approval_id) return failed('not executed: the task has no approval');
  const action = task.action;
  if (!action || !action.capability || !action.scope) {
    return failed('not executed: the task carries no scoped action (free-text requests are not executable); nothing ran');
  }
  if (!CAPABILITIES.includes(action.capability as RuntimeCapability) || task.capability !== action.capability) {
    return failed(`not executed: unsupported runtime capability ${String(action.capability || task.capability || 'unknown')}`);
  }
  if (!task.action_hash || (await actionHash(action)) !== task.action_hash) {
    return failed('not executed: the action does not match the one that was approved');
  }
  const workspace = String(action.scope.workspace ?? '.');
  const relativePath = String(action.scope.relative_path ?? '.');
  try {
    return await EXECUTORS[action.capability as RuntimeCapability].run({ workspace, relativePath }, invoke);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error || 'runtime execution failed');
    return failed(`action failed: ${message}`);
  }
}
