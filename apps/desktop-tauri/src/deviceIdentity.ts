import { invoke } from '@tauri-apps/api/core';

export interface DesktopDeviceIdentity {
  deviceId: string;
  publicKeyPem: string;
}

async function secureGet(key: string): Promise<string> {
  try { return await invoke<string>('secure_get', { key }); } catch { return ''; }
}

async function secureSet(key: string, value: string): Promise<void> {
  await invoke('secure_set', { key, value });
}

function randomHex(bytes: number): string {
  const array = new Uint8Array(bytes);
  crypto.getRandomValues(array);
  return Array.from(array).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function pemWrap(label: string, base64: string): string {
  const lines = base64.match(/.{1,64}/g)?.join('\n') || base64;
  return `-----BEGIN ${label}-----\n${lines}\n-----END ${label}-----`;
}

async function loadPrivateKey(): Promise<CryptoKey> {
  const privateJwk = await secureGet('omni.devicePrivateKeyJwk.v2');
  if (!privateJwk) throw new Error('desktop device private key is not initialized');
  return crypto.subtle.importKey(
    'jwk',
    JSON.parse(privateJwk),
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign'],
  );
}

export async function signDesktopDeviceRequest(deviceId: string, method: string, path: string, body = ''): Promise<Record<string, string>> {
  const privateKey = await loadPrivateKey();
  const timestamp = Date.now().toString();
  const nonce = randomHex(24);
  const bodyHash = await sha256Hex(body || '');
  const message = `omnidesk-device-request:v1:${method.toUpperCase()}:${path}:${bodyHash}:${timestamp}:${nonce}`;
  const signature = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    privateKey,
    new TextEncoder().encode(message),
  );
  return {
    'x-omnidesk-device-id': deviceId,
    'x-omnidesk-timestamp': timestamp,
    'x-omnidesk-nonce': nonce,
    'x-omnidesk-device-signature': `base64:${arrayBufferToBase64(signature)}`,
  };
}

export function createDesktopDeviceRequestSigner(deviceId: string) {
  return (method: string, path: string, body = '') => signDesktopDeviceRequest(deviceId, method, path, body);
}

export async function loadOrCreateDesktopIdentity(): Promise<DesktopDeviceIdentity> {
  const existingDeviceId = await secureGet('omni.deviceId.v2');
  const existingPublicKey = await secureGet('omni.devicePublicKeyPem.v2');
  const existingPrivateKey = await secureGet('omni.devicePrivateKeyJwk.v2');
  if (existingDeviceId && existingPublicKey && existingPrivateKey) {
    return { deviceId: existingDeviceId, publicKeyPem: existingPublicKey };
  }

  const keyPair = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    true,
    ['sign', 'verify'],
  );
  const publicSpki = await crypto.subtle.exportKey('spki', keyPair.publicKey);
  const privateJwk = await crypto.subtle.exportKey('jwk', keyPair.privateKey);
  const deviceId = `desk_${randomHex(18)}`;
  const publicKeyPem = pemWrap('PUBLIC KEY', arrayBufferToBase64(publicSpki));
  await secureSet('omni.deviceId.v2', deviceId);
  await secureSet('omni.devicePublicKeyPem.v2', publicKeyPem);
  await secureSet('omni.devicePrivateKeyJwk.v2', JSON.stringify(privateJwk));
  return { deviceId, publicKeyPem };
}
