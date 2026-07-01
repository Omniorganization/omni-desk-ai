import { assertCsrf, omniProxy } from '@/lib/session';

export async function POST(
  request: Request,
  context: { params: Promise<{ enrollmentId: string }> },
) {
  await assertCsrf();
  const { enrollmentId } = await context.params;
  return omniProxy(`/app/devices/enrollment/${encodeURIComponent(enrollmentId)}/challenge`, {
    method: 'POST',
    body: await request.text(),
    headers: {
      'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID(),
    },
  });
}
