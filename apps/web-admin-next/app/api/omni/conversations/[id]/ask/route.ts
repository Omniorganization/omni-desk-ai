import { assertCsrf, omniProxy } from '@/lib/session';

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  await assertCsrf();
  const { id } = await context.params;
  return omniProxy(`/app/conversations/${encodeURIComponent(id)}/ask`, {
    method: 'POST',
    body: await request.text(),
    headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() }
  });
}
