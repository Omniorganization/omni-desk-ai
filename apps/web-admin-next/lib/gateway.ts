const DEFAULT_GATEWAY_URL = 'http://127.0.0.1:18789';

export interface GatewayEnv {
  OMNI_GATEWAY_URL?: string;
  OMNI_GATEWAY_URL_ALLOWLIST?: string;
  OMNI_ALLOW_CLIENT_GATEWAY_URLS?: string;
  NODE_ENV?: string;
}

function flagEnabled(value: string | undefined): boolean {
  return value === '1' || value?.toLowerCase() === 'true';
}

function normalizeGatewayUrl(value: string): string {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('gateway URL must use http or https');
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('gateway URL must not contain credentials, query, or fragment');
  }
  const path = url.pathname === '/' ? '' : url.pathname.replace(/\/+$/, '');
  return `${url.protocol}//${url.host}${path}`;
}

function allowlist(env: GatewayEnv): Set<string> {
  const configured = env.OMNI_GATEWAY_URL || DEFAULT_GATEWAY_URL;
  const values = [configured, ...(env.OMNI_GATEWAY_URL_ALLOWLIST || '').split(',')];
  return new Set(values.map((value) => value.trim()).filter(Boolean).map(normalizeGatewayUrl));
}

function isLoopbackGateway(value: string): boolean {
  const host = new URL(value).hostname.toLowerCase();
  return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]';
}

export function resolveGatewayBaseUrl(candidate: string | undefined, env: GatewayEnv = process.env): string {
  const configured = normalizeGatewayUrl(env.OMNI_GATEWAY_URL || DEFAULT_GATEWAY_URL);
  if (!candidate?.trim()) return configured;

  const normalized = normalizeGatewayUrl(candidate.trim());
  const allowed = allowlist(env);
  const clientOverrideAllowed = flagEnabled(env.OMNI_ALLOW_CLIENT_GATEWAY_URLS);
  const devLoopbackAllowed = env.NODE_ENV !== 'production' && isLoopbackGateway(normalized);

  if (allowed.has(normalized)) return normalized;
  if (clientOverrideAllowed && devLoopbackAllowed) return normalized;
  return configured;
}
