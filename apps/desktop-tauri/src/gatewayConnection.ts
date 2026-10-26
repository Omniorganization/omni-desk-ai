import { DeviceRequestSigner, OmniApiClient, normalizeGatewayBaseUrl } from './api';

/** What the connection form currently shows. Never used to send a request. */
export interface GatewaySettings {
  gatewayUrl: string;
  token: string;
  actor: string;
}

export type GatewayDraftState = 'disconnected' | 'pending' | 'confirmed';

/**
 * A gateway the operator confirmed with "connect", or one a previous confirmed
 * connect saved to the keychain. API clients can only be built from this type,
 * so the operator token and device-signed requests never follow the text of
 * the URL field while it is being edited.
 */
export class GatewayTarget {
  private constructor(readonly baseUrl: string, readonly token: string, readonly actor: string) {}

  /** Validates the form before anything is sent or persisted. */
  static fromSettings(settings: GatewaySettings): GatewayTarget {
    return new GatewayTarget(normalizeGatewayBaseUrl(settings.gatewayUrl), settings.token, settings.actor);
  }

  /** Keychain values resume only with a saved token and a URL that still validates. */
  static restore(saved: GatewaySettings): GatewayTarget | null {
    if (!saved.gatewayUrl || !saved.token) return null;
    try {
      return GatewayTarget.fromSettings(saved);
    } catch {
      return null;
    }
  }

  /** True while the form still shows exactly this gateway, token and actor. */
  matches(settings: GatewaySettings): boolean {
    try {
      const draft = GatewayTarget.fromSettings(settings);
      return draft.baseUrl === this.baseUrl && draft.token === this.token && draft.actor === this.actor;
    } catch {
      return false;
    }
  }

  /** Same gateway and actor, so tasks and conversations from one still belong to the other. */
  sameGatewayAs(other: GatewayTarget | null): boolean {
    return other !== null && other.baseUrl === this.baseUrl && other.actor === this.actor;
  }

  client(deviceSigner?: DeviceRequestSigner): OmniApiClient {
    return new OmniApiClient({ baseUrl: this.baseUrl, token: this.token, actor: this.actor, deviceSigner });
  }
}

export function gatewayDraftState(confirmed: GatewayTarget | null, draft: GatewaySettings): GatewayDraftState {
  if (!confirmed) return 'disconnected';
  return confirmed.matches(draft) ? 'confirmed' : 'pending';
}

export interface GatewayBackgroundLoopOptions {
  confirmed: GatewayTarget | null;
  /** Read on every tick, since the form can change between ticks. */
  draft: () => GatewaySettings;
  intervalMs: number;
  tick: (gateway: GatewayTarget) => Promise<void>;
  onError: (error: unknown) => void;
}

/**
 * Runs heartbeat, bootstrap and task claims against the confirmed gateway only,
 * and skips every tick while the form holds unconfirmed changes. Returns a stop function.
 */
export function startGatewayBackgroundLoop(options: GatewayBackgroundLoopOptions): () => void {
  const { confirmed } = options;
  if (!confirmed) return () => {};
  const timer = setInterval(() => {
    if (!confirmed.matches(options.draft())) return;
    options.tick(confirmed).catch(options.onError);
  }, options.intervalMs);
  return () => clearInterval(timer);
}
