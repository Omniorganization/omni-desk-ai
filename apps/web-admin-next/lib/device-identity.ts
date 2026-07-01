'use client';

export interface WebAdminDeviceIdentity {
  deviceId: string;
  publicKeyPem: string;
}

interface StoredWebAdminIdentity extends WebAdminDeviceIdentity {
  privateKey: CryptoKey;
}

const DB_NAME = 'omnidesk-web-admin-device-identity';
const DB_VERSION = 1;
const STORE_NAME = 'identity';
const IDENTITY_KEY = 'current';

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

async function openIdentityDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };
    request.onerror = () => reject(request.error || new Error('failed to open web admin identity db'));
    request.onsuccess = () => resolve(request.result);
  });
}

async function getStoredIdentity(): Promise<StoredWebAdminIdentity | null> {
  const db = await openIdentityDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const request = tx.objectStore(STORE_NAME).get(IDENTITY_KEY);
    request.onerror = () => reject(request.error || new Error('failed to read web admin identity'));
    request.onsuccess = () => resolve((request.result as StoredWebAdminIdentity | undefined) || null);
    tx.oncomplete = () => db.close();
    tx.onerror = () => {
      db.close();
      reject(tx.error || new Error('web admin identity read transaction failed'));
    };
  });
}

async function putStoredIdentity(identity: StoredWebAdminIdentity): Promise<void> {
  const db = await openIdentityDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).put(identity, IDENTITY_KEY);
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => {
      db.close();
      reject(tx.error || new Error('web admin identity write transaction failed'));
    };
  });
}

async function createStoredIdentity(): Promise<StoredWebAdminIdentity> {
  const generated = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    true,
    ['sign', 'verify'],
  );
  const publicSpki = await crypto.subtle.exportKey('spki', generated.publicKey);
  const privateJwk = await crypto.subtle.exportKey('jwk', generated.privateKey);
  const privateKey = await crypto.subtle.importKey(
    'jwk',
    privateJwk,
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign'],
  );
  return {
    deviceId: `web_${randomHex(18)}`,
    publicKeyPem: pemWrap('PUBLIC KEY', arrayBufferToBase64(publicSpki)),
    privateKey,
  };
}

async function loadPrivateKey(): Promise<StoredWebAdminIdentity> {
  const existing = await getStoredIdentity();
  if (existing?.deviceId && existing.publicKeyPem && existing.privateKey) {
    return existing;
  }
  const created = await createStoredIdentity();
  await putStoredIdentity(created);
  return created;
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
