import type { OmniApiClient } from './api';
import type { DesktopDeviceIdentity } from './deviceIdentity';

// A fresh desktop becomes challenge-verified the way the gateway requires in
// production: an owner starts an enrollment for a desktop (Web Admin's devices
// card, or POST /app/devices/enrollment/start) and hands the operator its id and
// pairing code; this desktop binds its device id and public key with the code,
// then signs the challenge with its device key, which never leaves Rust.

export interface EnrollmentOutcome {
  deviceId: string;
  trustLevel: string;
}

export type ChallengeSigner = (signingMessage: string) => Promise<string>;

/** True for the gateway's refusal of a device that is not challenge-verified yet. */
export function needsEnrollment(error: unknown): boolean {
  return error instanceof Error && /device_not_challenge_verified/.test(error.message);
}

export async function enrollDesktop(options: {
  client: OmniApiClient;
  identity: DesktopDeviceIdentity;
  enrollmentId: string;
  pairingCode: string;
  signChallenge: ChallengeSigner;
}): Promise<EnrollmentOutcome> {
  const enrollmentId = options.enrollmentId.trim();
  const pairingCode = options.pairingCode.trim();
  if (!/^enroll_[0-9a-f]{16}$/.test(enrollmentId)) throw new Error('enrollment id must look like enroll_ followed by 16 hex digits');
  if (pairingCode.length < 8) throw new Error('pairing code must have at least 8 characters');
  const { client, identity } = options;
  await client.completeEnrollment(enrollmentId, pairingCode, identity.deviceId, identity.publicKeyPem);
  const issued = await client.enrollmentChallenge(enrollmentId, identity.deviceId);
  const challenge = issued?.challenge;
  if (!challenge?.challenge_id || !challenge?.signing_message) throw new Error('gateway returned no enrollment challenge');
  const signature = await options.signChallenge(String(challenge.signing_message));
  const verified = await client.verifyEnrollment(enrollmentId, String(challenge.challenge_id), identity.deviceId, signature);
  const trustLevel = String(verified?.credential?.trust_level || '');
  if (trustLevel !== 'challenge_verified') throw new Error('gateway did not verify this desktop');
  return { deviceId: identity.deviceId, trustLevel };
}
