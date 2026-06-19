import { omniProxy } from '@/lib/session';
export async function GET() { return omniProxy('/app/bootstrap'); }
