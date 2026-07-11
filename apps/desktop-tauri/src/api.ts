export type DeviceType = 'desktop' | 'mobile' | 'web_admin';
export type TaskStatus = 'queued' | 'running' | 'blocked' | 'completed' | 'failed' | 'cancelled';

export type DeviceRequestSigner = (method: string, path: string, body: string) => Promise<Record<string, string>>;

export interface OmniClientOptions {
  baseUrl: string;
  token: string;
  actor: string;
  deviceSigner?: DeviceRequestSigner;
}

export interface ProjectPayload {
  name?: string;
  description?: string;
  metadata?: Record<string, unknown>;
  archived?: boolean;
}

const EXECUTABLE_CAPABILITIES = new Set(['chat', 'local-runtime', 'shell_sandbox', 'dry_run']);

function normalizeCapabilities(capabilities: string[]): string[] {
  const normalized = capabilities.map(capability => capability === 'sandbox' ? 'shell_sandbox' : capability);
  return [...new Set(normalized.filter(capability => EXECUTABLE_CAPABILITIES.has(capability)))];
}

function gatewayError(status: number, body: string): Error {
  let detail = '';
  try {
    const payload = JSON.parse(body) as { detail?: unknown; code?: unknown };
    if (typeof payload.code === 'string') detail = payload.code;
    else if (typeof payload.detail === 'string') detail = payload.detail;
    else if (payload.detail && typeof payload.detail === 'object') {
      const code = (payload.detail as Record<string, unknown>).code;
      if (typeof code === 'string') detail = code;
    }
  } catch {
    detail = status >= 500 ? 'gateway_unavailable' : body.slice(0, 160);
  }
  return new Error(`${status} ${detail || 'request_failed'}`);
}

export class OmniApiClient {
  constructor(private options: OmniClientOptions) {}

  private async request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
    const baseUrl = this.options.baseUrl.replace(/\/$/, '');
    const method = (init.method || 'GET').toString().toUpperCase();
    const body = typeof init.body === 'string' ? init.body : '';
    const signedHeaders = this.options.deviceSigner ? await this.options.deviceSigner(method, path, body) : {};
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${this.options.token}`,
        'x-omnidesk-actor': this.options.actor,
        ...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}),
        ...signedHeaders,
        ...(init.headers || {})
      }
    });
    if (!response.ok) throw gatewayError(response.status, await response.text());
    return response.json() as Promise<T>;
  }

  bootstrap() { return this.request<any>('/app/bootstrap'); }
  projects() { return this.request<any>('/app/projects'); }

  createProject(name: string, description = '', metadata: Record<string, unknown> = {}, sourceDeviceId?: string, idempotencyKey?: string) {
    return this.request<any>('/app/projects', { method: 'POST', body: JSON.stringify({ name, description, metadata, source_device_id: sourceDeviceId }) }, idempotencyKey || `desktop-project-create-${name.length}-${Date.now()}`);
  }

  updateProject(projectId: string, payload: ProjectPayload, idempotencyKey?: string) {
    return this.request<any>(`/app/projects/${encodeURIComponent(projectId)}`, { method: 'PATCH', body: JSON.stringify(payload) }, idempotencyKey || `desktop-project-update-${projectId}-${Date.now()}`);
  }

  deleteProject(projectId: string, idempotencyKey?: string) {
    return this.request<any>(`/app/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' }, idempotencyKey || `desktop-project-delete-${projectId}-${Date.now()}`);
  }

  createConversation(title: string, sourceDeviceId?: string, idempotencyKey?: string) {
    return this.request<any>('/app/conversations', { method: 'POST', body: JSON.stringify({ title, source_device_id: sourceDeviceId }) }, idempotencyKey || `desktop-conversation-${Date.now()}`);
  }

  listMessages(conversationId: string) { return this.request<any>(`/app/conversations/${encodeURIComponent(conversationId)}/messages`); }

  askConversation(conversationId: string, content: string, modelProfile = 'fast', sourceDeviceId?: string, idempotencyKey?: string) {
    return this.request<any>(`/app/conversations/${encodeURIComponent(conversationId)}/ask`, { method: 'POST', body: JSON.stringify({ content, model_profile: modelProfile, stream: false, source_device_id: sourceDeviceId }) }, idempotencyKey || `desktop-ask-${conversationId}-${content.length}-${Date.now()}`);
  }

  registerDesktop(deviceId: string, platform: string, capabilities: string[], publicKey?: string) {
    return this.request<any>('/app/devices/register', { method: 'POST', body: JSON.stringify({ device_id: deviceId, device_type: 'desktop', name: 'Omni Desktop Runtime', platform, public_key: publicKey, capabilities: normalizeCapabilities(capabilities) }) }, `desktop-register-${deviceId}`);
  }

  heartbeat(deviceId: string, status: 'online' | 'offline' | 'degraded', version: string, capabilities: string[], activeTaskId?: string) {
    return this.request<any>('/app/runtime/desktop/heartbeat', { method: 'POST', body: JSON.stringify({ device_id: deviceId, status, version, active_task_id: activeTaskId, capabilities: normalizeCapabilities(capabilities) }) });
  }

  claimTask(deviceId: string, capabilities: string[], leaseSeconds = 60) {
    return this.request<any>('/app/runtime/desktop/claim', { method: 'POST', body: JSON.stringify({ device_id: deviceId, capabilities: normalizeCapabilities(capabilities), lease_seconds: leaseSeconds }) });
  }

  updateTaskStatus(taskId: string, status: TaskStatus, resultSummary?: string, assignedRuntimeDeviceId?: string, idempotencyKey?: string) {
    return this.request<any>(`/app/tasks/${taskId}/status`, { method: 'POST', body: JSON.stringify({ status, result_summary: resultSummary, assigned_runtime_device_id: assignedRuntimeDeviceId }) }, idempotencyKey || `desktop-task-${taskId}-${status}-${Date.now()}`);
  }

  registerPushToken(deviceId: string, pushToken: string, platform = 'desktop') {
    return this.request<any>(`/app/devices/${deviceId}/push-token`, { method: 'POST', body: JSON.stringify({ push_token: pushToken, platform }) });
  }

  completeEnrollment(enrollmentId: string, pairingCode: string, deviceId: string, publicKey: string) {
    return this.request<any>(`/app/devices/enrollment/${enrollmentId}/complete`, { method: 'POST', body: JSON.stringify({ pairing_code: pairingCode, device_id: deviceId, public_key: publicKey }) });
  }

  sync(sinceSeq = 0) { return this.request<any>(`/app/sync?since_seq=${sinceSeq}`); }
}
