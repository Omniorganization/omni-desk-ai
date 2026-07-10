import { assertCsrf, omniProxy } from '@/lib/session';

export async function PATCH(
  request: Request,
  context: { params: Promise<{ projectId: string }> },
) {
  await assertCsrf();
  const { projectId } = await context.params;
  return omniProxy(`/app/projects/${encodeURIComponent(projectId)}`, {
    method: 'PATCH',
    body: await request.text(),
    headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() },
  });
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ projectId: string }> },
) {
  await assertCsrf();
  const { projectId } = await context.params;
  return omniProxy(`/app/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
    headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() },
  });
}
