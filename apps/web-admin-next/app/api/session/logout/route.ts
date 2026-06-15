import { NextResponse } from 'next/server';
import { ACTOR_COOKIE, CSRF_COOKIE, GATEWAY_COOKIE, ROLE_COOKIE, SESSION_COOKIE } from '@/lib/session';
export async function POST() {
  const res = NextResponse.json({ ok: true });
  for (const name of [SESSION_COOKIE, GATEWAY_COOKIE, ACTOR_COOKIE, ROLE_COOKIE, CSRF_COOKIE]) res.cookies.set(name, '', { httpOnly: true, sameSite: 'strict', secure: process.env.NODE_ENV === 'production', path: '/', maxAge: 0 });
  return res;
}
