import { NextResponse } from 'next/server';
import { gatewayBaseUrl, getCsrfToken, getSessionToken } from '@/lib/session';
import { verifyGatewayIdentity } from '@/lib/session-login';

export async function GET() {
  try {
    const identity = await verifyGatewayIdentity(await gatewayBaseUrl(), await getSessionToken());
    return NextResponse.json({ ok: true, ...identity, csrfToken: await getCsrfToken() }, {
      headers: { 'cache-control': 'no-store' },
    });
  } catch {
    return NextResponse.json({ ok: false, error: 'session expired or invalid' }, {
      status: 401, headers: { 'cache-control': 'no-store' },
    });
  }
}
