// Every Next -> gateway call originates from 127.0.0.1, so the gateway's per-IP
// limiter is shared by all users. Without a limit here, one anonymous client
// could exhaust the gateway's budget for /admin/session/identity and log every
// user out. Limit per client (from the nginx-set X-Forwarded-For) and in total,
// keeping login traffic well under the gateway's per-path budget so existing
// session checks keep working.

export interface LoginRateLimitOptions {
  windowMs: number;
  perClient: number;
  total: number;
}

const DEFAULTS: LoginRateLimitOptions = { windowMs: 60_000, perClient: 10, total: 60 };
const MAX_TRACKED_CLIENTS = 10_000;

export class LoginRateLimiter {
  private clients = new Map<string, number[]>();
  private all: number[] = [];

  constructor(private readonly options: LoginRateLimitOptions = DEFAULTS) {}

  /** Records an attempt and returns false when it must be rejected. */
  allow(clientKey: string, now = Date.now()): boolean {
    const cutoff = now - this.options.windowMs;
    this.all = this.all.filter((ts) => ts > cutoff);
    const recent = (this.clients.get(clientKey) || []).filter((ts) => ts > cutoff);
    if (recent.length >= this.options.perClient || this.all.length >= this.options.total) {
      this.clients.set(clientKey, recent);
      return false;
    }
    recent.push(now);
    this.all.push(now);
    this.clients.set(clientKey, recent);
    if (this.clients.size > MAX_TRACKED_CLIENTS) this.prune(cutoff);
    return true;
  }

  private prune(cutoff: number) {
    for (const [key, stamps] of this.clients) {
      if (!stamps.some((ts) => ts > cutoff)) this.clients.delete(key);
    }
  }
}

/** nginx sets X-Forwarded-For to $remote_addr (overwriting any client value). */
export function loginClientKey(headers: Headers): string {
  const forwarded = headers.get('x-forwarded-for') || '';
  return forwarded.split(',')[0].trim().slice(0, 64) || 'unknown';
}

export const loginRateLimiter = new LoginRateLimiter();
