// A session cookie is only forwarded to the gateway when it carries a seal
// minted by this server's login route after the gateway verified the token.
// Arbitrary cookie values are rejected locally and never spend the gateway's
// pre-authentication budget.

const SEAL_VERSION = 'v1';
const CLOCK_SKEW_SECONDS = 60;
const DEFAULT_MAX_AGE_SECONDS = 3600;
const FALLBACK_KEY = Symbol.for('omnidesk.webAdmin.sessionSealKey');

// Reads OMNI_WEB_SESSION_SECRET, OMNI_WEB_SESSION_MAX_AGE_SECONDS and NODE_ENV.
export type SealEnv = Record<string, string | undefined>;

/** Everything login stored next to the token; a seal is only valid for this exact set. */
export interface SealedSession {
  token: string;
  gateway: string;
  actor: string;
  role: string;
}

export function sessionMaxAgeSeconds(env: SealEnv = process.env): number {
  const configured = Math.floor(Number(env.OMNI_WEB_SESSION_MAX_AGE_SECONDS));
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_MAX_AGE_SECONDS;
}

function keyMaterial(env: SealEnv): Uint8Array<ArrayBuffer> {
  const configured = env.OMNI_WEB_SESSION_SECRET || '';
  if (configured) {
    if (configured.length < 32) throw new Error('OMNI_WEB_SESSION_SECRET must be at least 32 characters');
    return new TextEncoder().encode(configured);
  }
  // Without a configured secret, sessions last until this process restarts.
  // globalThis is shared by the proxy and route bundles of one Node server.
  const holder = globalThis as typeof globalThis & { [FALLBACK_KEY]?: Uint8Array<ArrayBuffer> };
  if (!holder[FALLBACK_KEY] && env.NODE_ENV === 'production') {
    console.warn('OMNI_WEB_SESSION_SECRET is not set: web admin sessions end when this process restarts');
  }
  holder[FALLBACK_KEY] ??= crypto.getRandomValues(new Uint8Array(32));
  return holder[FALLBACK_KEY];
}

const keys = new Map<Uint8Array | string, Promise<CryptoKey>>();

function hmacKey(env: SealEnv): Promise<CryptoKey> {
  const material = keyMaterial(env);
  const cacheKey = env.OMNI_WEB_SESSION_SECRET || material;
  let key = keys.get(cacheKey);
  if (!key) {
    key = crypto.subtle.importKey('raw', material, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
    keys.set(cacheKey, key);
  }
  return key;
}

function sealedData(issuedAt: number, session: SealedSession): Uint8Array<ArrayBuffer> {
  const fields = [SEAL_VERSION, issuedAt, session.gateway, session.actor, session.role, session.token];
  return new TextEncoder().encode(JSON.stringify(fields));
}

export async function sealSession(
  session: SealedSession,
  issuedAt = Math.floor(Date.now() / 1000),
  env: SealEnv = process.env,
): Promise<string> {
  const mac = await crypto.subtle.sign('HMAC', await hmacKey(env), sealedData(issuedAt, session));
  return `${SEAL_VERSION}.${issuedAt}.${Buffer.from(mac).toString('base64url')}`;
}

export async function verifySessionSeal(
  session: Partial<SealedSession>,
  seal: string | undefined,
  now = Math.floor(Date.now() / 1000),
  env: SealEnv = process.env,
): Promise<boolean> {
  const match = /^v1\.([1-9]\d{0,11})\.([A-Za-z0-9_-]{43})$/.exec(seal || '');
  const { token, gateway, actor, role } = session;
  if (!match || !token || gateway === undefined || actor === undefined || role === undefined) return false;
  const issuedAt = Number(match[1]);
  if (issuedAt > now + CLOCK_SKEW_SECONDS || now - issuedAt > sessionMaxAgeSeconds(env)) return false;
  const mac = Buffer.from(match[2], 'base64url');
  if (mac.toString('base64url') !== match[2]) return false; // Only the canonical encoding is accepted.
  // subtle.verify compares the MAC in constant time.
  return crypto.subtle.verify('HMAC', await hmacKey(env), mac, sealedData(issuedAt, { token, gateway, actor, role }));
}
