export type AdminRole = 'viewer' | 'operator' | 'owner';
export type DeviceRequestSigner = (
  method: string,
  path: string,
  body: string,
) => Promise<Record<string, string>>;

export interface WebAdminDeviceRegistration {
  deviceId: string;
  publicKeyPem: string;
}

export interface ProjectPayload {
  name?: string;
  description?: string;
  metadata?: Record<string, unknown>;
  archived?: boolean;
}

export interface SessionOptions {
  baseUrl?: string;
  token?: string;
  csrfToken?: string;
  actor?: string;
  role?: AdminRole;
  deviceId?: string;
  publicKeyPem?: string;
  deviceSigner?: DeviceRequestSigner;
}

export class OmniAdminApi {
  constructor(private options: SessionOptions = {}) {}

  private get session(): SessionOptions & { csrfToken: string; actor: string; role: AdminRole } {
    return {
      ...this.options,
      baseUrl: this.options.baseUrl || '',
      token: '',
      csrfToken: this.options.csrfToken || '',
      actor: this.options.actor || 'web-admin',
      role: this.options.role || 'viewer',
    };
  }

  private async safeErrorMessage(response: Response): Promise<string> {
    let payload: unknown = {};
    try {
      payload = await response.clone().json();
    } catch {
      payload = {};
    }
    if (payload && typeof payload === 'object') {
      const record = payload as Record<string, unknown>;
      const detail = record.detail || record.error || record.code;
      if (typeof detail === 'string' && detail.length <= 160) {
        return `${response.status}: ${detail}`;
      }
    }
    return `${response.status}: request failed`;
  }

  private async request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
    const session = this.session;
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      headers: {
        'content-type': 'application/json',
        ...(session.csrfToken ? { 'x-csrf-token': session.csrfToken } : {}),
        ...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}),
        ...(init.headers || {})
      }
    });
    if (!response.ok) throw new Error(await this.safeErrorMessage(response));
    return response.json() as Promise<T>;
  }

  bootstrap() { return this.request<any>('/api/omni/bootstrap'); }
  runtime() { return this.request<any>('/api/omni/runtime'); }
  ecosystem() { return this.request<any>('/api/omni/channels/ecosystem'); }
  approvals() { return this.request<any>('/api/omni/approvals?status=pending'); }
  notifications() { return this.request<any>('/api/omni/notifications?audience=web_admin'); }
  conversations() { return this.request<any>('/api/omni/conversations'); }

  projects() { return this.request<any>('/api/omni/projects'); }
  createProject(
    name: string,
    description = '',
    metadata: Record<string, unknown> = {},
    extraPayload: Record<string, unknown> = {},
    idempotencyKey?: string,
  ) {
    const session = this.session;
    return this.request<any>('/api/omni/projects', {
      method: 'POST',
      body: JSON.stringify({
        name,
        description,
        metadata,
        ...extraPayload,
        ...(session.deviceId ? { source_device_id: session.deviceId } : {})
      })
    }, idempotencyKey || `web-admin-project-create-${name.length}-${Date.now()}`);
  }
  updateProject(projectId: string, payload: ProjectPayload, idempotencyKey?: string) {
    return this.request<any>(`/api/omni/projects/${encodeURIComponent(projectId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }, idempotencyKey || `web-admin-project-update-${projectId}-${Date.now()}`);
  }
  deleteProject(projectId: string, idempotencyKey?: string) {
    return this.request<any>(`/api/omni/projects/${encodeURIComponent(projectId)}`, {
      method: 'DELETE'
    }, idempotencyKey || `web-admin-project-delete-${projectId}-${Date.now()}`);
  }

  createConversation(title: string) {
    return this.request<any>('/api/omni/conversations', {
      method: 'POST',
      body: JSON.stringify({ title })
    }, `web-admin-conversation-${Date.now()}`);
  }
  listMessages(conversationId: string) {
    return this.request<any>(`/api/omni/conversations/${encodeURIComponent(conversationId)}/messages`);
  }
  askConversation(conversationId: string, content: string, modelProfile = 'fast') {
    const session = this.session;
    return this.request<any>(`/api/omni/conversations/${encodeURIComponent(conversationId)}/ask`, {
      method: 'POST',
      body: JSON.stringify({
        content,
        model_profile: modelProfile,
        stream: false,
        ...(session.deviceId ? { source_device_id: session.deviceId } : {})
      })
    }, `web-admin-ask-${conversationId}-${content.length}-${Date.now()}`);
  }
  registerAdminDevice(identity: WebAdminDeviceRegistration) {
    const session = this.session;
    return this.request<any>('/api/omni/devices/register', {
      method: 'POST',
      body: JSON.stringify({
        device_id: identity.deviceId,
        device_type: 'web_admin',
        name: 'Omni Web Admin',
        platform: 'nextjs',
        public_key: identity.publicKeyPem,
        organization_id: 'org_default',
        capabilities: ['governance', 'channels', 'audit', 'approval', `role:${session.role}`]
      })
    }, `web-admin-device-registration-${identity.deviceId}`);
  }
  async decide(approvalId: string, decision: 'approved' | 'rejected', reason = 'Web Admin decision') {
    const key = `web-admin-${approvalId}-${decision}-${Date.now()}`;
    const session = this.session;
    const gatewayPath = `/app/approvals/${encodeURIComponent(approvalId)}/decide`;
    const body = JSON.stringify({
      decision,
      reason,
      ...(session.deviceId ? { source_device_id: session.deviceId } : {})
    });
    const signedHeaders = session.deviceSigner
      ? await session.deviceSigner('POST', gatewayPath, body)
      : {};
    return this.request<any>(`/api/omni/approvals/${encodeURIComponent(approvalId)}/decide`, {
      method: 'POST',
      body,
      headers: signedHeaders,
    }, key);
  }

  startDeviceEnrollment(deviceType: 'desktop' | 'mobile' | 'web_admin', pairingCode: string) {
    return this.request<any>('/api/omni/devices/enrollment/start', {
      method: 'POST',
      body: JSON.stringify({ device_type: deviceType, pairing_code: pairingCode })
    }, `web-admin-enroll-${deviceType}-${Date.now()}`);
  }

  completeDeviceEnrollment(
    enrollmentId: string,
    pairingCode: string,
    identity: WebAdminDeviceRegistration,
  ) {
    return this.request<any>(`/api/omni/devices/enrollment/${encodeURIComponent(enrollmentId)}/complete`, {
      method: 'POST',
      body: JSON.stringify({
        pairing_code: pairingCode,
        device_id: identity.deviceId,
        public_key: identity.publicKeyPem,
      }),
    }, `web-admin-enroll-complete-${identity.deviceId}-${Date.now()}`);
  }

  issueDeviceChallenge(enrollmentId: string, deviceId: string) {
    return this.request<any>(`/api/omni/devices/enrollment/${encodeURIComponent(enrollmentId)}/challenge`, {
      method: 'POST',
      body: JSON.stringify({ device_id: deviceId }),
    }, `web-admin-enroll-challenge-${deviceId}-${Date.now()}`);
  }

  verifyDeviceChallenge(
    enrollmentId: string,
    challengeId: string,
    deviceId: string,
    signature: string,
  ) {
    return this.request<any>(`/api/omni/devices/enrollment/${encodeURIComponent(enrollmentId)}/verify`, {
      method: 'POST',
      body: JSON.stringify({
        challenge_id: challengeId,
        device_id: deviceId,
        signature,
      }),
    }, `web-admin-enroll-verify-${deviceId}-${Date.now()}`);
  }
}
