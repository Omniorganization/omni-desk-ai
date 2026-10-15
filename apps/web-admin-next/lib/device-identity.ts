'use client';

export interface WebAdminDeviceIdentity {
  deviceId: string;
  publicKeyPem: string;
}

interface SessionWebAdminIdentity extends WebAdminDeviceIdentity {
  privateKey: CryptoKey;
}

// Private keys exist only in this page's memory, never browser storage.
// A reload creates a new device identity and owner-approved enrollment.
let sessionIdentity: Promise<SessionWebAdminIdentity> | null = null;

export function resetWebAdminIdentity(): void {
  sessionIdentity = null;
}

function randomHex(bytes: number): string {
  const array = new Uint8Array(bytes);
  crypto.getRandomValues(array);
  return Array.from(array).map((value) => value.toString(16).padStart(2, '0')).join('');
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function pemWrap(label: string, base64: string): string {
  const lines = base64.match(/.{1,64}/g)?.join('\n') || base64;
  return `-----BEGIN ${label}-----\n${lines}\n-----END ${label}-----`;
}

async function createSessionIdentity(): Promise<SessionWebAdminIdentity> {
  const generated = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign', 'verify'],
  );
  const publicSpki = await crypto.subtle.exportKey('spki', generated.publicKey);
  return {
    deviceId: `web_${randomHex(18)}`,
    publicKeyPem: pemWrap('PUBLIC KEY', arrayBufferToBase64(publicSpki)),
    privateKey: generated.privateKey,
  };
}

async function loadPrivateKey(): Promise<SessionWebAdminIdentity> {
  if (!sessionIdentity) sessionIdentity = createSessionIdentity();
  try {
    return await sessionIdentity;
  } catch (error) {
    sessionIdentity = null;
    throw error;
  }
}

export async function loadOrCreateWebAdminIdentity(): Promise<WebAdminDeviceIdentity> {
  const identity = await loadPrivateKey();
  return { deviceId: identity.deviceId, publicKeyPem: identity.publicKeyPem };
}

export async function signWebAdminDeviceRequest(
  method: string,
  path: string,
  body = '',
): Promise<Record<string, string>> {
  const identity = await loadPrivateKey();
  const timestamp = Date.now().toString();
  const nonce = randomHex(24);
  const bodyHash = await sha256Hex(body || '');
  const message = `omnidesk-device-request:v1:${method.toUpperCase()}:${path}:${bodyHash}:${timestamp}:${nonce}`;
  const signature = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    identity.privateKey,
    new TextEncoder().encode(message),
  );
  return {
    'x-omnidesk-device-id': identity.deviceId,
    'x-omnidesk-timestamp': timestamp,
    'x-omnidesk-nonce': nonce,
    'x-omnidesk-device-signature': `base64:${arrayBufferToBase64(signature)}`,
  };
}

export async function signWebAdminChallenge(message: string): Promise<string> {
  const identity = await loadPrivateKey();
  const signature = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    identity.privateKey,
    new TextEncoder().encode(message),
  );
  return `base64:${arrayBufferToBase64(signature)}`;
}
