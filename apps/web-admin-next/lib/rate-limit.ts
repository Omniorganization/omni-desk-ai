// Per-client admission for every route that can reach the gateway. Keys come
// from the address nginx writes into X-Forwarded-For, so one client cannot
// spend another client's budget, and no global bucket exists that a handful of
// clients could fill to lock everyone out.

export interface RateLimitOptions {
  windowMs: number;
  limit: number;
  maxKeys: number;
}

export class FixedWindowRateLimiter {
  // Insertion order equals window start order: windows are only (re)opened by
  // delete + set, so the oldest windows are always at the front of the map.
  private windows = new Map<string, { start: number; count: number }>();

  constructor(private readonly options: RateLimitOptions) {}

  /** Records an admitted request; rejected requests never create state. */
  allow(key: string, now = Date.now()): boolean {
    const current = this.windows.get(key);
    if (current && now - current.start < this.options.windowMs) {
      if (current.count >= this.options.limit) return false;
      current.count += 1;
      return true;
    }
    if (current) this.windows.delete(key);
    this.makeRoom(now);
    this.windows.set(key, { start: now, count: 1 });
    return true;
  }

  get size(): number {
    return this.windows.size;
  }

  private makeRoom(now: number) {
    for (const [key, window] of this.windows) {
      if (this.windows.size < this.options.maxKeys) return;
      if (now - window.start < this.options.windowMs) break;
      this.windows.delete(key);
    }
    // Still full of live windows: drop the ones closest to expiry.
    for (const key of this.windows.keys()) {
      if (this.windows.size < this.options.maxKeys) return;
      this.windows.delete(key);
    }
  }
}

/** Header the gateway trusts only from its configured loopback proxies. */
export const CLIENT_IP_HEADER = 'x-omnidesk-client-ip';

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

function ipv6Hextets(value: string): number[] | null {
  if (!value.includes(':') || !/^[0-9A-Fa-f:.]+$/.test(value)) return null;
  let host: string;
  try {
    host = new URL(`http://[${value}]/`).hostname.slice(1, -1);
  } catch {
    return null;
  }
  // WHATWG URL serializes IPv6 as lower-case hex groups with at most one "::".
  const [head, tail = ''] = host.split('::');
  const left = head ? head.split(':') : [];
  const right = tail ? tail.split(':') : [];
  const groups = host.includes('::')
    ? [...left, ...Array(8 - left.length - right.length).fill('0'), ...right]
    : left;
  return groups.map((group) => parseInt(group, 16));
}

/**
 * The client address as seen by the nearest proxy: the rightmost
 * X-Forwarded-For entry (nginx writes $remote_addr). Left entries are
 * client-supplied and ignored. IPv4-mapped IPv6 is reported as IPv4.
 */
export function clientAddress(headers: Headers): string | null {
  const hops = (headers.get('x-forwarded-for') || '').split(',');
  const nearest = hops[hops.length - 1].trim();
  if (IPV4.test(nearest)) return nearest;
  const groups = ipv6Hextets(nearest);
  if (!groups) return null;
  if (groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff) {
    return [groups[6] >> 8, groups[6] & 0xff, groups[7] >> 8, groups[7] & 0xff].join('.');
  }
  return new URL(`http://[${nearest}]/`).hostname.slice(1, -1);
}

/** IPv6 clients are keyed per /64: one subscriber usually controls the whole prefix. */
export function clientRateLimitKey(address: string | null): string {
  if (!address) return 'unknown';
  const groups = ipv6Hextets(address);
  if (!groups) return `ip:${address}`;
  return `ip6:${groups.slice(0, 4).map((group) => group.toString(16)).join(':')}::/64`;
}
