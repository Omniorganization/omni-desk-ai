import { cookies, headers } from 'next/headers';
import { resolveGatewayBaseUrl } from './gateway';

export const SESSION_COOKIE = '__Host-omni_session_token';
export const CSRF_COOKIE = '__Host-omni_csrf_token';
export const GATEWAY_COOKIE = '__Host-omni_gateway_url';
export const ACTOR_COOKIE = '__Host-omni_actor';
export const ROLE_COOKIE = '__Host-omni_role';

const DEVICE_SIGNATURE_HEADERS = [
  'x-omnidesk-device-id',
  'x-omnidesk-timestamp',
  'x-omnidesk-nonce',
  'x-omnidesk-device-signature',
];

export async function getSessionToken(): Promise<string> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value || '';
  if (!token) throw new Error('missing session token');
  return token;
}
export async function getCsrfToken(): Promise<string> { const jar = await cookies(); return jar.get(CSRF_COOKIE)?.value || ''; }

export async function assertCsrf(): Promise<void> {
  const expected = await getCsrfToken();
  const actual = (await headers()).get('x-csrf-token') || '';
  if (!expected || actual !== expected) throw new Error('csrf token mismatch');
}

export async function gatewayBaseUrl(): Promise<string> {
  const jar = await cookies();
  return resolveGatewayBaseUrl(jar.get(GATEWAY_COOKIE)?.value, process.env);
}

export function deviceSignatureHeaders(request: Request): Record<string, string> {
  const forwarded: Record<string, string> = {};
  for (const headerName of DEVICE_SIGNATURE_HEADERS) {
    const value = request.headers.get(headerName);
    if (value) forwarded[headerName] = value;
  }
  return forwarded;
}

export async function omniProxy(path: string, init: RequestInit = {}) {
  const base = (await gatewayBaseUrl()).replace(/\/$/, '');
  const token = await getSessionToken();
  const jar = await cookies();
  const actor = jar.get(ACTOR_COOKIE)?.value || 'web-admin';
  const role = jar.get(ROLE_COOKIE)?.value || 'viewer';
  const forwarded = new Headers(init.headers);
  forwarded.set('content-type', 'application/json');
  forwarded.set('authorization', `Bearer ${token}`);
  forwarded.set('x-omnidesk-actor', actor);
  forwarded.set('x-omnidesk-client-role', role);
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: forwarded,
    cache: 'no-store'
  });
  const contentType = response.headers.get('content-type') || 'application/json';
  if (contentType.includes('text/event-stream')) {
    const responseHeaders = new Headers({
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': response.headers.get('cache-control') || 'no-cache, no-transform',
      'x-accel-buffering': response.headers.get('x-accel-buffering') || 'no',
      connection: 'keep-alive',
    });
    return new Response(response.body, {
      status: response.status,
      headers: responseHeaders,
    });
  }
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: { 'content-type': contentType },
  });
}
