import { invoke } from '@tauri-apps/api/core';

// The device private key lives only in the Rust side of the app: it is
// generated, stored and used for signing there, and never returned to the
// webview. This module only sees the device id, the public key and signatures.

export interface DesktopDeviceIdentity {
  deviceId: string;
  publicKeyPem: string;
}

function randomHex(bytes: number): string {
  const array = new Uint8Array(bytes);
  crypto.getRandomValues(array);
  return Array.from(array).map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function signDesktopDeviceRequest(deviceId: string, method: string, path: string, body = ''): Promise<Record<string, string>> {
  const timestamp = Date.now().toString();
  const nonce = randomHex(24);
  const signature = await invoke<string>('device_sign_request', {
    method: method.toUpperCase(),
    path,
    bodySha256: await sha256Hex(body || ''),
    timestamp,
    nonce,
  });
  return {
    'x-omnidesk-device-id': deviceId,
    'x-omnidesk-timestamp': timestamp,
    'x-omnidesk-nonce': nonce,
    'x-omnidesk-device-signature': `base64:${signature}`,
  };
}

export function createDesktopDeviceRequestSigner(deviceId: string) {
  return (method: string, path: string, body = '') => signDesktopDeviceRequest(deviceId, method, path, body);
}

let pendingIdentity: Promise<DesktopDeviceIdentity> | undefined;

/** Loads the device identity, creating it in Rust (under a cross-process lock) on first use. */
export function loadOrCreateDesktopIdentity(): Promise<DesktopDeviceIdentity> {
  if (!pendingIdentity) {
    pendingIdentity = invoke<DesktopDeviceIdentity>('device_identity').finally(() => { pendingIdentity = undefined; });
  }
  return pendingIdentity;
}
