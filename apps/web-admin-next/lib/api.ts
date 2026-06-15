export type AdminRole = 'viewer' | 'operator' | 'owner';

export interface SessionOptions {
  baseUrl?: string;
  token?: string;
  csrfToken?: string;
  actor?: string;
  role?: AdminRole;
}

export class OmniAdminApi {
  constructor(private options: SessionOptions = {}) {}

  private get session(): Required<SessionOptions> {
    return { baseUrl: this.options.baseUrl || '', token: '', csrfToken: this.options.csrfToken || '', actor: this.options.actor || 'web-admin', role: this.options.role || 'viewer' };
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
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    return response.json() as Promise<T>;
  }

  bootstrap() { return this.request<any>('/api/omni/bootstrap'); }
  ecosystem() { return this.request<any>('/api/omni/channels/ecosystem'); }
  approvals() { return this.request<any>('/api/omni/approvals?status=pending'); }
  notifications() { return this.request<any>('/api/omni/notifications?audience=web_admin'); }
  registerAdminDevice() {
    const session = this.session;
    return this.request<any>('/api/omni/devices/register', {
      method: 'POST',
      body: JSON.stringify({
        device_id: 'web-admin-console',
        device_type: 'web_admin',
        name: 'Omni Web Admin',
        platform: 'nextjs',
        organization_id: 'org_default',
        capabilities: ['governance', 'channels', 'audit', 'approval', `role:${session.role}`]
      })
    }, 'web-admin-device-registration');
  }
  decide(approvalId: string, decision: 'approved' | 'rejected', reason = 'Web Admin decision') {
    const key = `web-admin-${approvalId}-${decision}-${Date.now()}`;
    return this.request<any>(`/api/omni/approvals/${approvalId}/decide`, {
      method: 'POST',
      body: JSON.stringify({ decision, reason })
    }, key);
  }

  startDeviceEnrollment(deviceType: 'desktop' | 'mobile' | 'web_admin', pairingCode: string) {
    return this.request<any>('/api/omni/devices/enrollment/start', {
      method: 'POST',
      body: JSON.stringify({ device_type: deviceType, pairing_code: pairingCode })
    }, `web-admin-enroll-${deviceType}-${Date.now()}`);
  }
}
