import { invoke } from '@tauri-apps/api/core';

export type RuntimeCapability = 'dry_run' | 'shell_sandbox' | 'file_operation';
export interface RuntimeTask {
  task_id: string;
  title: string;
  approval_id?: string;
  capability?: RuntimeCapability;
  scope?: Record<string, unknown>;
  timeout_seconds?: number;
  artifact_policy?: 'none' | 'summary' | 'upload';
  network_policy?: 'none' | 'approved_only';
  filesystem_policy?: 'none' | 'workspace_only';
}
export interface ExecutionResult {
  status: 'completed' | 'failed' | 'cancelled';
  summary: string;
  artifacts?: string[];
}
export interface RuntimeExecutor {
  capability: RuntimeCapability;
  canExecute(task: RuntimeTask): boolean;
  execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult>;
}

function requireApprovalScope(task: RuntimeTask): Record<string, unknown> {
  if (!task.approval_id) throw new Error('approved approval_id is required');
  if (!task.scope) throw new Error('approval scope is required');
  if (task.filesystem_policy !== 'workspace_only') throw new Error('workspace_only filesystem policy is required');
  if (task.network_policy !== 'none') throw new Error('network_policy none is required');
  if (task.artifact_policy !== 'summary') throw new Error('summary artifact policy is required');
  return task.scope;
}

function relativePathFromScope(scope: Record<string, unknown>): string {
  const relativePath = String(scope.relative_path || '.');
  if (!relativePath || relativePath.length > 1024 || relativePath.includes('\0')) {
    throw new Error('relative_path is invalid or exceeds 1024 characters');
  }
  return relativePath;
}

function requireWorkspace(scope: Record<string, unknown>): string {
  const workspace = String(scope.workspace || '');
  if (!workspace) throw new Error('approved workspace is required');
  return workspace;
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException('Runtime task cancelled', 'AbortError');
}

export class DryRunExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'dry_run';
  canExecute(task: RuntimeTask): boolean {
    return !task.capability || task.capability === this.capability;
  }
  async execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult> {
    throwIfAborted(signal);
    return {
      status: 'completed',
      summary: `Desktop dry-run accepted ${task.task_id}: ${task.title.slice(0, 160)}`,
    };
  }
}

export class ShellSandboxExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'shell_sandbox';
  canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; }
  async execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult> {
    const scope = requireApprovalScope(task);
    const workspace = requireWorkspace(scope);
    const operation = String(scope.operation || '');
    const relativePath = relativePathFromScope(scope);
    throwIfAborted(signal);
    if (operation === 'read_file') {
      const output = await invoke<string>('read_workspace_file', { workspace, relativePath });
      throwIfAborted(signal);
      return {
        status: 'completed',
        summary: `workspace read completed (${output.length} characters; contents and path omitted from status)`,
      };
    }
    if (operation === 'list_directory') {
      const entries = await invoke<string[]>('list_workspace_directory', { workspace, relativePath });
      throwIfAborted(signal);
      return {
        status: 'completed',
        summary: `workspace list completed (${entries.length} entries; names and path omitted from status)`,
      };
    }
    throw new Error('shell_sandbox only supports read_file or list_directory');
  }
}

export class FileOperationExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'file_operation';
  canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; }
  async execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult> {
    const scope = requireApprovalScope(task);
    const workspace = requireWorkspace(scope);
    const operation = String(scope.operation || '');
    const relativePath = relativePathFromScope(scope);
    throwIfAborted(signal);

    if (operation === 'write_file') {
      const content = String(scope.content ?? '');
      const expectedSha256 = scope.expected_sha256 == null ? null : String(scope.expected_sha256);
      const digest = await invoke<string>('write_workspace_file', {
        workspace,
        relativePath,
        content,
        expectedSha256,
      });
      throwIfAborted(signal);
      return { status: 'completed', summary: `workspace write completed (sha256=${digest})` };
    }
    if (operation === 'patch_file') {
      const expectedSha256 = String(scope.expected_sha256 || '');
      const find = String(scope.find ?? '');
      const replace = String(scope.replace ?? '');
      const digest = await invoke<string>('patch_workspace_file', {
        workspace,
        relativePath,
        expectedSha256,
        find,
        replace,
      });
      throwIfAborted(signal);
      return { status: 'completed', summary: `workspace patch completed (sha256=${digest})` };
    }
    if (operation === 'diff_file') {
      const proposedContent = String(scope.proposed_content ?? '');
      const diff = await invoke<string>('diff_workspace_file', {
        workspace,
        relativePath,
        proposedContent,
      });
      throwIfAborted(signal);
      return {
        status: 'completed',
        summary: `workspace diff completed (${diff.split('\n').length} bounded lines; content omitted from status)`,
        artifacts: [diff],
      };
    }
    throw new Error('file_operation supports write_file, patch_file, or diff_file');
  }
}

export const EXECUTORS: RuntimeExecutor[] = [
  new ShellSandboxExecutor(),
  new FileOperationExecutor(),
  new DryRunExecutor(),
];

export function advertisedRuntimeCapabilities(): string[] {
  return ['chat', 'local-runtime', ...EXECUTORS.map(executor => executor.capability)];
}

export async function executeRuntimeTask(
  task: RuntimeTask,
  signal?: AbortSignal,
): Promise<ExecutionResult> {
  const executor = EXECUTORS.find(item => item.canExecute(task));
  if (!executor) {
    return {
      status: 'failed',
      summary: `unsupported runtime capability: ${String(task.capability || 'unknown')}`,
    };
  }
  try {
    return await executor.execute(task, signal);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { status: 'cancelled', summary: 'runtime task cancelled before completion' };
    }
    const message = error instanceof Error ? error.message : 'runtime execution failed';
    return { status: 'failed', summary: message.slice(0, 500) };
  }
}
