import type { OmniApiClient, TaskStatus } from './api';
import type { ExecutionResult, RuntimeTask } from './executor';

const OUTBOX_KEY = 'omnidesk.desktop.runtime-status-outbox.v1';
const LEASE_SECONDS = 120;
const LEASE_RENEW_INTERVAL_MS = 30_000;

export interface PendingStatusReport {
  reportId: string;
  taskId: string;
  status: TaskStatus;
  summary: string;
  deviceId: string;
  attempts: number;
  createdAt: string;
  nextRetryAt: number;
}

export interface WorkerCycleHooks {
  onClaimed?: (task: RuntimeTask | null) => void;
  onLog?: (message: string) => void;
  onSnapshot?: (snapshot: unknown) => void;
}

export interface WorkerCycleResult {
  task: RuntimeTask | null;
  execution?: ExecutionResult;
  statusReported: boolean;
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readStatusOutbox(): PendingStatusReport[] {
  const target = storage();
  if (!target) return [];
  try {
    const parsed = JSON.parse(target.getItem(OUTBOX_KEY) || '[]') as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((item): item is PendingStatusReport => Boolean(
          item &&
          typeof item === 'object' &&
          typeof (item as PendingStatusReport).reportId === 'string' &&
          typeof (item as PendingStatusReport).taskId === 'string',
        )).slice(-200)
      : [];
  } catch {
    return [];
  }
}

function writeStatusOutbox(items: PendingStatusReport[]): void {
  const target = storage();
  if (!target) return;
  target.setItem(OUTBOX_KEY, JSON.stringify(items.slice(-200)));
}

export function enqueueStatusReport(report: Omit<PendingStatusReport, 'reportId' | 'attempts' | 'createdAt' | 'nextRetryAt'>): PendingStatusReport {
  const item: PendingStatusReport = {
    ...report,
    reportId: crypto.randomUUID(),
    attempts: 0,
    createdAt: new Date().toISOString(),
    nextRetryAt: Date.now(),
  };
  writeStatusOutbox([...readStatusOutbox(), item]);
  return item;
}

export async function flushStatusOutbox(client: OmniApiClient): Promise<number> {
  const pending = readStatusOutbox();
  if (!pending.length) return 0;
  const remaining: PendingStatusReport[] = [];
  let delivered = 0;
  for (const item of pending) {
    if (item.nextRetryAt > Date.now()) {
      remaining.push(item);
      continue;
    }
    try {
      await client.updateTaskStatus(
        item.taskId,
        item.status,
        item.summary,
        item.deviceId,
        `desktop-outbox-${item.reportId}`,
      );
      delivered += 1;
    } catch {
      const attempts = item.attempts + 1;
      remaining.push({
        ...item,
        attempts,
        nextRetryAt: Date.now() + Math.min(300_000, 2 ** Math.min(attempts, 8) * 1000),
      });
    }
  }
  writeStatusOutbox(remaining);
  return delivered;
}

function timeoutMilliseconds(task: RuntimeTask): number {
  const seconds = Number(task.timeout_seconds || 120);
  return Math.max(5, Math.min(Number.isFinite(seconds) ? seconds : 120, 600)) * 1000;
}

async function reportOrQueue(
  client: OmniApiClient,
  task: RuntimeTask,
  execution: ExecutionResult,
  deviceId: string,
): Promise<boolean> {
  const status: TaskStatus = execution.status;
  const key = `desktop-execution-${task.task_id}-${status}-${crypto.randomUUID()}`;
  try {
    await client.updateTaskStatus(task.task_id, status, execution.summary, deviceId, key);
    return true;
  } catch {
    enqueueStatusReport({
      taskId: task.task_id,
      status,
      summary: execution.summary,
      deviceId,
    });
    return false;
  }
}

export async function runDurableWorkerCycle(
  client: OmniApiClient,
  deviceId: string,
  capabilities: string[],
  execute: (task: RuntimeTask, signal: AbortSignal) => Promise<ExecutionResult>,
  hooks: WorkerCycleHooks = {},
): Promise<WorkerCycleResult> {
  await flushStatusOutbox(client);
  const claimed = await client.claimTask(deviceId, capabilities, LEASE_SECONDS);
  const task = (claimed.task || null) as RuntimeTask | null;
  hooks.onClaimed?.(task);
  if (!task) {
    return { task: null, statusReported: true };
  }

  const abortController = new AbortController();
  let leaseFailure: unknown = null;
  const timeout = window.setTimeout(() => abortController.abort('task_timeout'), timeoutMilliseconds(task));
  const leaseTimer = window.setInterval(() => {
    void (async () => {
      try {
        const control = await client.taskControl(task.task_id, deviceId);
        if (control.control.cancel_requested || control.control.lease_expired) {
          abortController.abort(control.control.cancel_requested ? 'server_cancelled' : 'lease_expired');
          return;
        }
        await client.renewTaskLease(task.task_id, deviceId, LEASE_SECONDS);
      } catch (error) {
        leaseFailure = error;
        abortController.abort('lease_renewal_failed');
      }
    })();
  }, LEASE_RENEW_INTERVAL_MS);

  let execution: ExecutionResult;
  try {
    execution = await execute(task, abortController.signal);
    if (leaseFailure && execution.status === 'cancelled') {
      execution = {
        status: 'failed',
        summary: 'runtime execution stopped because lease renewal failed',
      };
    } else if (abortController.signal.aborted && execution.status !== 'failed') {
      execution = {
        status: 'cancelled',
        summary: `runtime task cancelled (${String(abortController.signal.reason || 'cancelled')})`,
      };
    }
  } finally {
    window.clearTimeout(timeout);
    window.clearInterval(leaseTimer);
  }

  const statusReported = await reportOrQueue(client, task, execution, deviceId);
  hooks.onLog?.(`${new Date().toISOString()} ${execution.status}: ${execution.summary}`);
  hooks.onClaimed?.(null);
  try {
    hooks.onSnapshot?.(await client.bootstrap());
  } catch {
    // Status is already durable in the local outbox; snapshot refresh is best effort.
  }
  return { task, execution, statusReported };
}
