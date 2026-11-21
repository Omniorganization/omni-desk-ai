import { cookies, headers } from 'next/headers';
import { resolveGatewayBaseUrl } from './gateway';
import { CONTRACT_HEADER, CONTRACT_VERSION, ContractMismatchError, assertCompatibleGateway, assertContractResponse } from './generated/contract';
import { CLIENT_IP_HEADER, clientAddress } from './rate-limit';
import { verifySessionSeal } from './session-seal';

import { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE, SESSION_SEAL_COOKIE } from './session-names';
export { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE, SESSION_SEAL_COOKIE } from './session-names';

const DEVICE_SIGNATURE_HEADERS = [
  'x-omnidesk-device-id',
  'x-omnidesk-timestamp',
  'x-omnidesk-nonce',
  'x-omnidesk-device-signature',
];

export class SessionRequiredError extends Error {}

/** The session token, only if this server sealed it at login (proxy.ts checks too; this does not depend on routing). */
export async function getSessionToken(): Promise<string> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value || '';
  const session = { token, gateway: jar.get(GATEWAY_COOKIE)?.value, actor: jar.get(ACTOR_COOKIE)?.value, role: jar.get(ROLE_COOKIE)?.value };
  if (!(await verifySessionSeal(session, jar.get(SESSION_SEAL_COOKIE)?.value))) throw new SessionRequiredError('session required');
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

/** The browser's address, forwarded so the gateway can key quotas per client. */
export async function gatewayClientIp(): Promise<string | null> {
  return clientAddress(await headers());
}

export function deviceSignatureHeaders(request: Request): Record<string, string> {
  const forwarded: Record<string, string> = {};
  for (const headerName of DEVICE_SIGNATURE_HEADERS) {
    const value = request.headers.get(headerName);
    if (value) forwarded[headerName] = value;
  }
  return forwarded;
}

export function gatewayRequestHeaders(
  init: HeadersInit | undefined,
  token: string,
  actor: string,
  role: string,
  clientIp: string | null,
): Headers {
  const forwarded = new Headers(init);
  forwarded.set('content-type', 'application/json');
  forwarded.set('authorization', `Bearer ${token}`);
  forwarded.set('x-omnidesk-actor', actor);
  forwarded.set('x-omnidesk-client-role', role);
  forwarded.set(CONTRACT_HEADER, CONTRACT_VERSION);
  // Always this server's view of the browser address, never a caller-supplied value.
  if (clientIp) forwarded.set(CLIENT_IP_HEADER, clientIp);
  else forwarded.delete(CLIENT_IP_HEADER);
  return forwarded;
}

/**
 * Why a gateway answer breaks the API contract this Web Admin was built for:
 * another major version, or a successful JSON answer without a field the UI
 * reads. Null when it meets the contract.
 */
export function gatewayContractFailure(method: string, path: string, response: Response, bodyText: string | null): string | null {
  try {
    assertCompatibleGateway(response.headers.get(CONTRACT_HEADER));
    if (response.ok && bodyText !== null) assertContractResponse(method, path, JSON.parse(bodyText));
    return null;
  } catch (error) {
    if (error instanceof ContractMismatchError) return error.message.slice(0, 160);
    if (error instanceof SyntaxError) return 'gateway answer is not JSON';
    throw error;
  }
}

export async function omniProxy(path: string, init: RequestInit = {}) {
  const base = (await gatewayBaseUrl()).replace(/\/$/, '');
  let token: string;
  try {
    token = await getSessionToken();
  } catch (error) {
    if (error instanceof SessionRequiredError) return Response.json({ ok: false, error: 'session required' }, { status: 401 });
    throw error;
  }
  const jar = await cookies();
  const actor = jar.get(ACTOR_COOKIE)?.value || 'web-admin';
  const role = jar.get(ROLE_COOKIE)?.value || 'viewer';
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: gatewayRequestHeaders(init.headers, token, actor, role, await gatewayClientIp()),
    cache: 'no-store'
  });
  const contentType = response.headers.get('content-type') || 'application/json';
  const method = (init.method || 'GET').toString().toUpperCase();
  if (contentType.includes('text/event-stream')) {
    const failure = gatewayContractFailure(method, path, response, null);
    if (failure) return Response.json({ ok: false, error: 'gateway_contract_mismatch', detail: failure }, { status: 502 });
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
  const failure = gatewayContractFailure(method, path, response, contentType.includes('json') ? body : null);
  if (failure) return Response.json({ ok: false, error: 'gateway_contract_mismatch', detail: failure }, { status: 502 });
  return new Response(body, {
    status: response.status,
    headers: { 'content-type': contentType },
  });
}
