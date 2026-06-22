export type SessionRole = 'viewer' | 'operator' | 'owner';

const ROLES = new Set<SessionRole>(['viewer', 'operator', 'owner']);

export interface GatewayIdentity {
  actor: string;
  role: SessionRole;
}

export function normalizeGatewayActor(value: unknown): string {
  const raw = String(value || '').trim();
  const safe = Array.from(raw).filter((ch) => /[A-Za-z0-9@._\-:]/.test(ch)).join('').slice(0, 128);
  return safe || 'web-admin';
}

export function normalizeGatewayRole(value: unknown): SessionRole {
  const role = String(value || 'viewer') as SessionRole;
  return ROLES.has(role) ? role : 'viewer';
}

export async function verifyGatewayIdentity(
  gatewayUrl: string,
  token: string,
  fetchImpl: typeof fetch = fetch,
): Promise<GatewayIdentity> {
  const response = await fetchImpl(`${gatewayUrl.replace(/\/$/, '')}/admin/session/identity`, {
    method: 'GET',
    headers: { authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  if (!response.ok) {
    throw new Error(`gateway identity verification failed: ${response.status}`);
  }
  const text = await response.text();
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(text) as Record<string, unknown>;
  } catch {
    throw new Error('gateway identity response was not valid JSON');
  }
  return {
    actor: normalizeGatewayActor(payload.actor),
    role: normalizeGatewayRole(payload.role),
  };
}
