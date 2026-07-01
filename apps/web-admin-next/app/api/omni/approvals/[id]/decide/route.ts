import { assertCsrf, deviceSignatureHeaders, omniProxy } from '@/lib/session';

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  await assertCsrf();
  const { id } = await context.params;
  return omniProxy(`/app/approvals/${encodeURIComponent(id)}/decide`, {
    method: 'POST',
    body: await request.text(),
    headers: {
      ...deviceSignatureHeaders(request),
      'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID(),
    },
  });
}
