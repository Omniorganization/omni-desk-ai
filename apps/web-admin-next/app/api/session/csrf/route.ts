import { NextResponse } from 'next/server';
import { getCsrfToken } from '@/lib/session';
export async function GET() { return NextResponse.json({ ok: true, csrfToken: await getCsrfToken() }); }
