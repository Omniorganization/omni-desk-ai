import { assertCsrf, deviceSignatureHeaders, omniProxy } from '@/lib/session';

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  await assertCsrf();
  const { deviceId } = await context.params;
  return omniProxy(`/app/devices/${encodeURIComponent(deviceId)}/revoke`, {
    method: 'POST',
    body: await request.text(),
    headers: {
      ...deviceSignatureHeaders(request),
      'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID(),
    },
  });
}
