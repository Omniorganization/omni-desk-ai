import { clientAddress, clientRateLimitKey, FixedWindowRateLimiter } from './rate-limit';
import { verifySessionSeal, type SealEnv, type SealedSession } from './session-seal';

// Per client and minute, logins plus session routes (10 + 120) stay near the
// gateway's per-client budgets (300 per IP, 120 per IP and path), so this
// server sheds one client's abuse before it reaches the gateway.
export const LOGIN_LIMIT = { windowMs: 60_000, limit: 10, maxKeys: 10_000 };
export const SESSION_LIMIT = { windowMs: 60_000, limit: 120, maxKeys: 10_000 };

/** Routes whose handlers call the gateway with the session token. */
export function isGatewaySessionRoute(path: string): boolean {
  return path === '/api/session/current' || path.startsWith('/api/omni/');
}

/**
 * The path Next routes on: it percent-decodes once before matching route
 * files, so admission decisions must use the same decoded form.
 */
export function routedPath(pathname: string): string | null {
  try {
    return decodeURIComponent(pathname);
  } catch {
    return null;
  }
}

export interface AdmissionRejection {
  status: 401 | 429;
  error: string;
  retryAfterSeconds?: number;
}

export function tooMany(error: string, windowMs: number): AdmissionRejection {
  return { status: 429, error, retryAfterSeconds: Math.ceil(windowMs / 1000) };
}

/** Login handler's limiter; counted in the POST handler so other methods and path spellings cannot skip or spend it. */
export const loginRateLimiter = new FixedWindowRateLimiter(LOGIN_LIMIT);

export class SessionAdmission {
  constructor(
    private readonly limiter = new FixedWindowRateLimiter(SESSION_LIMIT),
    private readonly env: SealEnv = process.env,
  ) {}

  async admit(path: string, headers: Headers, session: Partial<SealedSession>, seal: string | undefined, now = Date.now()): Promise<AdmissionRejection | null> {
    if (!isGatewaySessionRoute(path)) return null;
    // Unsealed cookies are rejected before they cost a gateway call or this
    // client's budget (which other users behind the same address share).
    if (!(await verifySessionSeal(session, seal, Math.floor(now / 1000), this.env))) {
      return { status: 401, error: 'session required' };
    }
    const key = clientRateLimitKey(clientAddress(headers));
    return this.limiter.allow(key, now) ? null : tooMany('too many requests', SESSION_LIMIT.windowMs);
  }
}
