import { NextResponse } from 'next/server';
import crypto from 'node:crypto';
import { resolveGatewayBaseUrl } from '@/lib/gateway';
import { verifyGatewayIdentity } from '@/lib/session-login';
import { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE } from '@/lib/session';
export async function POST(request: Request) {
  const payload = await request.json();
  const token = String(payload.token || '');
  const gatewayUrl = resolveGatewayBaseUrl(String(payload.gatewayUrl || ''), process.env);
  if (!token) return NextResponse.json({ ok: false, error: 'token required' }, { status: 422 });
  let identity;
  try {
    identity = await verifyGatewayIdentity(gatewayUrl, token);
  } catch {
    return NextResponse.json({ ok: false, error: 'invalid gateway token' }, { status: 401 });
  }
  const { actor, role } = identity;
  const csrf = crypto.randomBytes(24).toString('hex');
  const res = NextResponse.json({ ok: true, csrfToken: csrf, actor, role });
  const secure = process.env.NODE_ENV === 'production';
  const maxAge = Number(process.env.OMNI_WEB_SESSION_MAX_AGE_SECONDS || 3600);
  const cookies: Array<[string, string]> = [[SESSION_COOKIE, token], [GATEWAY_COOKIE, gatewayUrl], [ACTOR_COOKIE, actor], [ROLE_COOKIE, role], [CSRF_COOKIE, csrf]];
  for (const [name, value] of cookies) {
    res.cookies.set(name, value, { httpOnly: true, sameSite: 'strict', secure, path: '/', maxAge });
  }
  return res;
}
