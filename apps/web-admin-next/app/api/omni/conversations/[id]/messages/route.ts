import { assertCsrf, omniProxy } from '@/lib/session';

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return omniProxy(`/app/conversations/${encodeURIComponent(id)}/messages`);
}

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  await assertCsrf();
  const { id } = await context.params;
  return omniProxy(`/app/conversations/${encodeURIComponent(id)}/messages`, {
    method: 'POST',
    body: await request.text(),
    headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() }
  });
}
