import { NextRequest, NextResponse } from 'next/server';
import { CSRF_COOKIE, SESSION_COOKIE } from './lib/session-names';

export function proxy(request: NextRequest) {
  const path = request.nextUrl.pathname;
  const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(request.method);
  if (path.startsWith('/api/')) {
    if (mutation) {
      const origin = request.headers.get('origin');
      const expected = process.env.OMNI_WEB_PUBLIC_ORIGIN || request.nextUrl.origin;
      if (!origin || origin !== expected) {
        return NextResponse.json({ ok: false, error: 'origin rejected' }, { status: 403 });
      }
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
