import { invoke } from '@tauri-apps/api/core';

export type RuntimeCapability = 'dry_run' | 'shell_sandbox' | 'browser_automation' | 'file_operation' | 'ui_bridge';
export interface RuntimeTask { task_id: string; title: string; approval_id?: string; capability?: RuntimeCapability; scope?: Record<string, unknown>; timeout_seconds?: number; artifact_policy?: 'none' | 'summary' | 'upload'; network_policy?: 'none' | 'approved_only'; filesystem_policy?: 'none' | 'workspace_only'; }
export interface ExecutionResult { status: 'completed' | 'failed'; summary: string; artifacts?: string[]; }
export interface RuntimeExecutor { capability: RuntimeCapability; canExecute(task: RuntimeTask): boolean; execute(task: RuntimeTask): Promise<ExecutionResult>; }

function requireApprovalScope(task: RuntimeTask): void {
  if (!task.approval_id) throw new Error('approved approval_id is required');
  if (!task.scope) throw new Error('approval scope is required');
}

function relativePathFromScope(scope: Record<string, unknown>): string {
  const relativePath = String(scope.relative_path || '.');
  if (!relativePath || relativePath.length > 1024 || relativePath.includes('\0')) {
    throw new Error('relative_path is invalid or exceeds 1024 characters');
  }
  return relativePath;
}

export class DryRunExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'dry_run';
  canExecute(task: RuntimeTask): boolean { return !task.capability || task.capability === this.capability; }
  async execute(task: RuntimeTask): Promise<ExecutionResult> {
    return { status: 'completed', summary: `Desktop dry-run accepted ${task.task_id}: ${task.title.slice(0, 160)}` };
  }
}

export class ShellSandboxExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'shell_sandbox';
  canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; }
  async execute(task: RuntimeTask): Promise<ExecutionResult> {
    requireApprovalScope(task);
    if (task.filesystem_policy !== 'workspace_only') throw new Error('shell_sandbox requires workspace_only filesystem policy');
    if (task.network_policy !== 'none') throw new Error('shell_sandbox requires network_policy none');
    if (task.artifact_policy !== 'summary') throw new Error('shell_sandbox requires summary artifact policy');
    const scope = task.scope || {};
    const workspace = String(scope.workspace || '');
    const operation = String(scope.operation || '');
    const relativePath = relativePathFromScope(scope);
    if (!workspace) throw new Error('approved workspace is required');
    if (operation === 'read_file') {
      const output = await invoke<string>('read_workspace_file', { workspace, relativePath });
      return { status: 'completed', summary: `workspace read completed (${output.length} characters; contents and path omitted from status)` };
    }
    if (operation === 'list_directory') {
      const entries = await invoke<string[]>('list_workspace_directory', { workspace, relativePath });
      return { status: 'completed', summary: `workspace list completed (${entries.length} entries; names and path omitted from status)` };
    }
    throw new Error('shell_sandbox only supports read_file or list_directory');
  }
}

export const EXECUTORS: RuntimeExecutor[] = [new ShellSandboxExecutor(), new DryRunExecutor()];

export function advertisedRuntimeCapabilities(): string[] {
  return ['chat', 'local-runtime', ...EXECUTORS.map(executor => executor.capability)];
}

export async function executeRuntimeTask(task: RuntimeTask): Promise<ExecutionResult> {
  const executor = EXECUTORS.find(item => item.canExecute(task));
  if (!executor) return { status: 'failed', summary: `unsupported runtime capability: ${String(task.capability || 'unknown')}` };
  try {
    return await executor.execute(task);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'runtime execution failed';
    return { status: 'failed', summary: message.slice(0, 500) };
  }
}
