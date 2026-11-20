import { omniProxy } from '@/lib/session';

export async function GET(request: Request) {
  const since = Number(new URL(request.url).searchParams.get('since_seq') || 0);
  const sinceSeq = Number.isFinite(since) && since > 0 ? Math.floor(since) : 0;
  return omniProxy(`/app/sync?since_seq=${sinceSeq}`);
}
