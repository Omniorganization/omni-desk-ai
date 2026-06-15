import { invoke } from '@tauri-apps/api/core';

export type RuntimeCapability = 'dry_run' | 'shell_sandbox' | 'browser_automation' | 'file_operation' | 'ui_bridge';
export interface RuntimeTask { task_id: string; title: string; approval_id?: string; capability?: RuntimeCapability; scope?: Record<string, unknown>; timeout_seconds?: number; artifact_policy?: 'none' | 'summary' | 'upload'; network_policy?: 'none' | 'approved_only'; filesystem_policy?: 'none' | 'workspace_only'; }
export interface ExecutionResult { status: 'completed' | 'failed'; summary: string; artifacts?: string[]; }
export interface RuntimeExecutor { capability: RuntimeCapability; canExecute(task: RuntimeTask): boolean; execute(task: RuntimeTask): Promise<ExecutionResult>; }
function requireApprovalScope(task: RuntimeTask): void { if (!task.approval_id) throw new Error('approved approval_id is required'); if (!task.scope) throw new Error('approval scope is required'); }
function stringList(value: unknown): string[] { return Array.isArray(value) ? value.map(String) : []; }

export class DryRunExecutor implements RuntimeExecutor { capability: RuntimeCapability = 'dry_run'; canExecute(task: RuntimeTask): boolean { return !task.capability || task.capability === this.capability; } async execute(task: RuntimeTask): Promise<ExecutionResult> { return { status: 'completed', summary: `Desktop dry-run executor accepted ${task.task_id}: ${task.title}` }; } }
export class ShellSandboxExecutor implements RuntimeExecutor {
  capability: RuntimeCapability = 'shell_sandbox';
  canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; }
  async execute(task: RuntimeTask): Promise<ExecutionResult> {
    requireApprovalScope(task);
    if (task.filesystem_policy !== 'workspace_only') throw new Error('shell_sandbox requires workspace_only filesystem policy');
    if (task.network_policy !== 'none') throw new Error('shell_sandbox requires network_policy none');
    const scope = task.scope || {};
    const command = String(scope.command || '');
    const args = stringList(scope.args);
    const workspace = String(scope.workspace || '');
    const stdout = await invoke<string>('run_workspace_command', { workspace, command, args });
    return { status: 'completed', summary: `shell_sandbox completed ${command}: ${stdout.slice(0, 4000)}` };
  }
}
export class BrowserAutomationExecutor implements RuntimeExecutor { capability: RuntimeCapability = 'browser_automation'; canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; } async execute(task: RuntimeTask): Promise<ExecutionResult> { requireApprovalScope(task); return { status: 'failed', summary: 'browser_automation requires signed browser policy and remains gated in production-ga' }; } }
export class FileOperationExecutor implements RuntimeExecutor { capability: RuntimeCapability = 'file_operation'; canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; } async execute(task: RuntimeTask): Promise<ExecutionResult> { requireApprovalScope(task); return { status: 'failed', summary: 'file_operation requires artifact policy and remains gated in production-ga' }; } }
export class UiBridgeExecutor implements RuntimeExecutor { capability: RuntimeCapability = 'ui_bridge'; canExecute(task: RuntimeTask): boolean { return task.capability === this.capability; } async execute(task: RuntimeTask): Promise<ExecutionResult> { requireApprovalScope(task); return { status: 'failed', summary: 'ui_bridge requires OS permission policy and remains gated in production-ga' }; } }
export const EXECUTORS: RuntimeExecutor[] = [new ShellSandboxExecutor(), new DryRunExecutor(), new BrowserAutomationExecutor(), new FileOperationExecutor(), new UiBridgeExecutor()];
export async function executeRuntimeTask(task: RuntimeTask): Promise<ExecutionResult> { const executor = EXECUTORS.find(item => item.canExecute(task)) || EXECUTORS[1]; return executor.execute(task); }
