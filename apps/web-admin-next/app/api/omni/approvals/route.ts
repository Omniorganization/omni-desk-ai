import { omniProxy } from '@/lib/session';
export async function GET(request: Request) { const url = new URL(request.url); return omniProxy(`/app/approvals${url.search}`); }
