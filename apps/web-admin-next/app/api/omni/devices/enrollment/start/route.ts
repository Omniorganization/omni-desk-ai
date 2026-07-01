import { assertCsrf, omniProxy } from '@/lib/session';

export async function POST(request: Request) {
  await assertCsrf();
  return omniProxy('/app/devices/enrollment/start', {
    method: 'POST',
    body: await request.text(),
    headers: {
      'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID(),
    },
  });
}
