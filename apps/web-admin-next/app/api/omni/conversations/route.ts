import { assertCsrf, omniProxy } from '@/lib/session';

export async function GET() {
  return omniProxy('/app/conversations');
}

export async function POST(request: Request) {
  await assertCsrf();
  return omniProxy('/app/conversations', {
    method: 'POST',
    body: await request.text(),
    headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() }
  });
}
