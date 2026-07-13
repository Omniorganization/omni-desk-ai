import { assertCsrf, omniProxy } from '@/lib/session';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export async function POST(request: Request) {
  await assertCsrf();
  return omniProxy('/api/chat/stream', {
    method: 'POST',
    body: await request.text(),
    headers: {
      accept: 'text/event-stream',
      'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID(),
      ...(request.headers.get('last-event-id')
        ? { 'last-event-id': request.headers.get('last-event-id') as string }
        : {}),
    },
    signal: request.signal,
  });
}
