import { NextRequest, NextResponse } from 'next/server';
import { routedPath, SessionAdmission } from './lib/api-admission';
import { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE, SESSION_SEAL_COOKIE } from './lib/session-names';

// One instance per server process: gateway-bound session requests are
// admitted here before any route handler can call the gateway.
const admission = new SessionAdmission();

export async function proxy(request: NextRequest) {
  const path = routedPath(request.nextUrl.pathname);
  if (path === null) return NextResponse.json({ ok: false, error: 'invalid path' }, { status: 400 });
  const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(request.method);
  if (path.startsWith('/api/')) {
    if (mutation) {
      const origin = request.headers.get('origin');
      const expected = process.env.OMNI_WEB_PUBLIC_ORIGIN || request.nextUrl.origin;
      if (!origin || origin !== expected) {
        return NextResponse.json({ ok: false, error: 'origin rejected' }, { status: 403 });
      }
    }
    const cookie = (name: string) => request.cookies.get(name)?.value;
    const rejection = await admission.admit(path, request.headers, {
      token: cookie(SESSION_COOKIE), gateway: cookie(GATEWAY_COOKIE), actor: cookie(ACTOR_COOKIE), role: cookie(ROLE_COOKIE),
    }, cookie(SESSION_SEAL_COOKIE));
    if (rejection) {
      const headers: Record<string, string> = { 'cache-control': 'no-store' };
      if (rejection.retryAfterSeconds) headers['retry-after'] = String(rejection.retryAfterSeconds);
      return NextResponse.json({ ok: false, error: rejection.error }, { status: rejection.status, headers });
    }
    const protectedRoute = path.startsWith('/api/omni/') || path === '/api/session/logout';
    if (protectedRoute && !request.cookies.get(SESSION_COOKIE)?.value) {
      return NextResponse.json({ ok: false, error: 'session required' }, { status: 401 });
    }
    if (protectedRoute && mutation) {
      const expected = request.cookies.get(CSRF_COOKIE)?.value;
      if (!expected || request.headers.get('x-csrf-token') !== expected) {
        return NextResponse.json({ ok: false, error: 'csrf token mismatch' }, { status: 403 });
      }
    }
    const response = NextResponse.next();
    response.headers.set('cache-control', 'no-store');
    return response;
  }
  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const csp = [
    "default-src 'self'", "connect-src 'self'", "img-src 'self'",
    `style-src 'self' 'nonce-${nonce}'`,
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'",
    "require-trusted-types-for 'script'", 'trusted-types default nextjs', 'upgrade-insecure-requests',
  ].join('; ');
  const headers = new Headers(request.headers);
  headers.set('x-nonce', nonce);
  headers.set('content-security-policy', csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('content-security-policy', csp);
  response.headers.set('cache-control', 'private, no-store');
  return response;
}

export const config = { matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'] };
