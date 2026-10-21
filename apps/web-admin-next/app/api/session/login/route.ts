import { NextResponse } from 'next/server';
import crypto from 'node:crypto';
import { LOGIN_LIMIT, loginRateLimiter } from '@/lib/api-admission';
import { resolveGatewayBaseUrl } from '@/lib/gateway';
import { clientAddress, clientRateLimitKey } from '@/lib/rate-limit';
import { isPlausibleGatewayToken, verifyGatewayIdentity } from '@/lib/session-login';
import { sealSession, sessionMaxAgeSeconds } from '@/lib/session-seal';
import { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE, SESSION_SEAL_COOKIE } from '@/lib/session';
export async function POST(request: Request) {
  const clientIp = clientAddress(request.headers);
  if (!loginRateLimiter.allow(clientRateLimitKey(clientIp))) {
    return NextResponse.json({ ok: false, error: 'too many login attempts' }, { status: 429, headers: { 'retry-after': String(LOGIN_LIMIT.windowMs / 1000) } });
  }
  const payload = await request.json();
  const token = String(payload.token || '');
  const gatewayUrl = resolveGatewayBaseUrl(String(payload.gatewayUrl || ''), process.env);
  if (!token) return NextResponse.json({ ok: false, error: 'token required' }, { status: 422 });
  if (!isPlausibleGatewayToken(token)) return NextResponse.json({ ok: false, error: 'invalid gateway token' }, { status: 401 });
  let identity;
  try {
    identity = await verifyGatewayIdentity(gatewayUrl, token, fetch, clientIp);
  } catch {
    return NextResponse.json({ ok: false, error: 'invalid gateway token' }, { status: 401 });
  }
  const { actor, role } = identity;
  const csrf = crypto.randomBytes(24).toString('hex');
  const res = NextResponse.json({ ok: true, csrfToken: csrf, actor, role });
  const secure = process.env.NODE_ENV === 'production';
  const maxAge = sessionMaxAgeSeconds();
  const seal = await sealSession({ token, gateway: gatewayUrl, actor, role });
  const cookies: Array<[string, string]> = [[SESSION_COOKIE, token], [SESSION_SEAL_COOKIE, seal], [GATEWAY_COOKIE, gatewayUrl], [ACTOR_COOKIE, actor], [ROLE_COOKIE, role], [CSRF_COOKIE, csrf]];
  for (const [name, value] of cookies) {
    res.cookies.set(name, value, { httpOnly: true, sameSite: 'strict', secure, path: '/', maxAge });
  }
  return res;
}
