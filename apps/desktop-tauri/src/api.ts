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

export interface ChatStreamEvent {
  id: number;
  event: 'chat.started' | 'chat.delta' | 'chat.reasoning.delta' | 'chat.usage' | 'chat.completed' | 'chat.failed';
  data: Record<string, unknown>;
}

export interface StreamChatOptions {
  conversationId?: string;
  content: string;
  modelProfile?: string;
  sourceDeviceId?: string;
  idempotencyKey?: string;
  lastEventId?: number;
  signal?: AbortSignal;
  onEvent?: (event: ChatStreamEvent) => void;
}

const EXECUTABLE_CAPABILITIES = new Set(['chat', 'local-runtime', 'shell_sandbox', 'dry_run']);

function normalizeCapabilities(capabilities: string[]): string[] {
  const normalized = capabilities.map(capability => capability === 'sandbox' ? 'shell_sandbox' : capability);
  return [...new Set(normalized.filter(capability => EXECUTABLE_CAPABILITIES.has(capability)))];
}

function gatewayError(status: number, body: string): Error {
  if (status >= 500) return new Error(`${status} gateway_unavailable`);

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
    detail = body.slice(0, 160);
  }
  return new Error(`${status} ${detail || 'request_failed'}`);
}

function parseSseBlock(block: string): ChatStreamEvent | null {
  let id = 0;
  let event = '';
  const data: string[] = [];
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'id') id = Number(value);
    else if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (!event || !Number.isSafeInteger(id) || id < 1) return null;
  let payload: Record<string, unknown> = {};
  try {
    const decoded = JSON.parse(data.join('\n')) as unknown;
    if (decoded && typeof decoded === 'object' && !Array.isArray(decoded)) {
      payload = decoded as Record<string, unknown>;
    }
  } catch {
    payload = { code: 'invalid_stream_event' };
  }
  return { id, event: event as ChatStreamEvent['event'], data: payload };
}

export class OmniApiClient {
  constructor(private options: OmniClientOptions) {}

  private async headers(path: string, method: string, body: string, idempotencyKey?: string): Promise<Record<string, string>> {
    const signedHeaders = this.options.deviceSigner ? await this.options.deviceSigner(method, path, body) : {};
    return {
      'content-type': 'application/json',
      authorization: `Bearer ${this.options.token}`,
      'x-omnidesk-actor': this.options.actor,
      ...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}),
      ...signedHeaders,
    };
  }

  private async request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
    const baseUrl = this.options.baseUrl.replace(/\/$/, '');
    const method = (init.method || 'GET').toString().toUpperCase();
    const body = typeof init.body === 'string' ? init.body : '';
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        ...await this.headers(path, method, body, idempotencyKey),
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

  async streamChat(options: StreamChatOptions): Promise<{ lastEventId: number; completed: boolean }> {
    const path = '/api/chat/stream';
    const body = JSON.stringify({
      conversation_id: options.conversationId,
      content: options.content,
      model_profile: options.modelProfile || 'fast',
      source_device_id: options.sourceDeviceId,
    });
    const key = options.idempotencyKey || `desktop-stream-${options.conversationId || 'new'}-${crypto.randomUUID()}`;
    const response = await fetch(`${this.options.baseUrl.replace(/\/$/, '')}${path}`, {
      method: 'POST',
      body,
      signal: options.signal,
      headers: {
        ...await this.headers(path, 'POST', body, key),
        accept: 'text/event-stream',
        ...(options.lastEventId ? { 'last-event-id': String(options.lastEventId) } : {}),
      },
    });
    if (!response.ok) throw gatewayError(response.status, await response.text());
    if (!response.body) throw new Error('stream_body_unavailable');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let lastEventId = options.lastEventId || 0;
    let completed = false;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseBlock(block);
        if (event && event.id > lastEventId) {
          lastEventId = event.id;
          options.onEvent?.(event);
          if (event.event === 'chat.completed') completed = true;
          if (event.event === 'chat.failed') throw new Error(String(event.data.code || 'chat_stream_failed'));
        }
        boundary = buffer.indexOf('\n\n');
      }
      if (done) break;
    }
    return { lastEventId, completed };
  }

  async streamChatWithFallback(options: StreamChatOptions): Promise<{ streamed: boolean; result?: any; lastEventId: number }> {
    let observed = false;
    try {
      const result = await this.streamChat({
        ...options,
        onEvent: event => {
          observed = true;
          options.onEvent?.(event);
        },
      });
      return { streamed: true, lastEventId: result.lastEventId };
    } catch (error) {
      if (observed || options.signal?.aborted || !options.conversationId) throw error;
      const result = await this.askConversation(
        options.conversationId,
        options.content,
        options.modelProfile,
        options.sourceDeviceId,
        options.idempotencyKey,
      );
      return { streamed: false, result, lastEventId: options.lastEventId || 0 };
    }
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
