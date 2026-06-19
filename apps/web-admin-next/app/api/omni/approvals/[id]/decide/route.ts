import { assertCsrf, omniProxy } from '@/lib/session';
export async function POST(request: Request, context: { params: Promise<{ id: string }> }) { await assertCsrf(); const { id } = await context.params; return omniProxy(`/app/approvals/${id}/decide`, { method: 'POST', body: await request.text(), headers: { 'idempotency-key': request.headers.get('idempotency-key') || crypto.randomUUID() } }); }
