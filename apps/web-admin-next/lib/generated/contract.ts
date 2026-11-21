// Generated from apps/shared/omni-app-api.contract.json by scripts/generate_contract_clients.py. Do not edit; run the script.

export const CONTRACT_VERSION = "1.0";
export const CONTRACT_HEADER = 'x-omnidesk-contract';

/** [field path, allowed JSON types]; `a[].b` is field b of every element of array a. */
export type ContractRequirement = readonly [string, readonly string[]];

export const RESPONSE_REQUIREMENTS: Readonly<Record<string, readonly ContractRequirement[]>> = {
  "GET /app/bootstrap": [
    ["", ["object"]],
    ["pending_approvals", ["array"]],
    ["pending_approvals[]", ["object"]],
    ["pending_approvals[].approval_id", ["string"]],
    ["pending_approvals[].action", ["string"]],
    ["pending_approvals[].risk", ["string"]],
    ["notifications", ["array"]],
    ["notifications[]", ["object"]],
    ["notifications[].notification_id", ["string"]],
    ["notifications[].title", ["string"]],
    ["notifications[].body", ["string"]],
    ["runtime_status", ["array"]],
    ["devices", ["array"]],
  ],
  "GET /app/projects": [
    ["", ["object"]],
    ["projects", ["array"]],
    ["projects[]", ["object"]],
    ["projects[].project_id", ["string"]],
    ["projects[].name", ["string"]],
    ["projects[].archived", ["boolean"]],
  ],
  "POST /app/projects": [
    ["", ["object"]],
    ["project", ["object"]],
    ["project.project_id", ["string"]],
    ["project.name", ["string"]],
    ["project.archived", ["boolean"]],
  ],
  "PATCH /app/projects/{project_id}": [
    ["", ["object"]],
  ],
  "DELETE /app/projects/{project_id}": [
    ["", ["object"]],
  ],
  "POST /app/devices/register": [
    ["", ["object"]],
    ["device", ["object"]],
    ["device.device_id", ["string"]],
    ["device.credential_status", ["string"]],
  ],
  "GET /app/conversations": [
    ["", ["object"]],
    ["conversations", ["array"]],
  ],
  "POST /app/conversations": [
    ["", ["object"]],
    ["conversation", ["object"]],
    ["conversation.conversation_id", ["string"]],
  ],
  "POST /app/conversations/{conversation_id}/messages": [
    ["", ["object"]],
  ],
  "GET /app/conversations/{conversation_id}/messages": [
    ["", ["object"]],
    ["messages", ["array"]],
    ["messages[]", ["object"]],
    ["messages[].role", ["string"]],
    ["messages[].content", ["string"]],
  ],
  "POST /app/conversations/{conversation_id}/ask": [
    ["", ["object"]],
    ["assistant_message", ["object"]],
    ["assistant_message.role", ["string"]],
    ["assistant_message.content", ["string"]],
  ],
  "POST /app/tasks/{task_id}/status": [
    ["", ["object"]],
  ],
  "GET /app/approvals": [
    ["", ["object"]],
    ["approvals", ["array"]],
  ],
  "POST /app/approvals/{approval_id}/decide": [
    ["", ["object"]],
  ],
  "GET /app/notifications": [
    ["", ["object"]],
    ["notifications", ["array"]],
    ["notifications[]", ["object"]],
    ["notifications[].notification_id", ["string"]],
    ["notifications[].title", ["string"]],
    ["notifications[].body", ["string"]],
  ],
  "POST /app/runtime/desktop/heartbeat": [
    ["", ["object"]],
  ],
  "POST /app/runtime/desktop/claim": [
    ["", ["object"]],
    ["task", ["object", "null"]],
    ["task.task_id", ["string"]],
    ["task.title", ["string"]],
    ["task.action", ["object"]],
  ],
  "GET /app/sync": [
    ["", ["object"]],
  ],
  "POST /app/devices/{device_id}/push-token": [
    ["", ["object"]],
  ],
  "POST /app/devices/{device_id}/approval-key": [
    ["", ["object"]],
  ],
  "POST /app/devices/enrollment/start": [
    ["", ["object"]],
    ["enrollment", ["object"]],
    ["enrollment.enrollment_id", ["string"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/complete": [
    ["", ["object"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/challenge": [
    ["", ["object"]],
    ["challenge", ["object"]],
    ["challenge.challenge_id", ["string"]],
    ["challenge.signing_message", ["string"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/verify": [
    ["", ["object"]],
    ["credential", ["object"]],
    ["credential.trust_level", ["string"]],
  ],
  "GET /admin/session/identity": [
    ["", ["object"]],
    ["actor", ["string"]],
    ["role", ["string"]],
  ],
  "GET /admin/channels/ecosystem": [
    ["", ["object"]],
    ["channels", ["array"]],
  ],
};

export const REQUEST_REQUIREMENTS: Readonly<Record<string, readonly ContractRequirement[]>> = {
  "POST /app/projects": [
    ["", ["object"]],
    ["name", ["string"]],
  ],
  "PATCH /app/projects/{project_id}": [
    ["", ["object"]],
  ],
  "POST /app/devices/register": [
    ["", ["object"]],
    ["device_id", ["string"]],
    ["device_type", ["string"]],
  ],
  "POST /app/conversations": [
    ["", ["object"]],
  ],
  "POST /app/conversations/{conversation_id}/messages": [
    ["", ["object"]],
    ["content", ["string"]],
  ],
  "POST /app/conversations/{conversation_id}/ask": [
    ["", ["object"]],
    ["content", ["string"]],
  ],
  "POST /api/chat/stream": [
    ["", ["object"]],
    ["content", ["string"]],
  ],
  "POST /app/tasks/{task_id}/status": [
    ["", ["object"]],
    ["status", ["string"]],
  ],
  "POST /app/approvals/{approval_id}/decide": [
    ["", ["object"]],
    ["decision", ["string"]],
  ],
  "POST /app/runtime/desktop/heartbeat": [
    ["", ["object"]],
    ["device_id", ["string"]],
    ["status", ["string"]],
  ],
  "POST /app/runtime/desktop/claim": [
    ["", ["object"]],
    ["device_id", ["string"]],
  ],
  "POST /app/devices/{device_id}/push-token": [
    ["", ["object"]],
    ["push_token", ["string"]],
  ],
  "POST /app/devices/{device_id}/approval-key": [
    ["", ["object"]],
    ["public_key", ["string"]],
  ],
  "POST /app/devices/enrollment/start": [
    ["", ["object"]],
    ["device_type", ["string"]],
    ["pairing_code", ["string"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/complete": [
    ["", ["object"]],
    ["pairing_code", ["string"]],
    ["device_id", ["string"]],
    ["public_key", ["string"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/challenge": [
    ["", ["object"]],
    ["device_id", ["string"]],
  ],
  "POST /app/devices/enrollment/{enrollment_id}/verify": [
    ["", ["object"]],
    ["challenge_id", ["string"]],
    ["device_id", ["string"]],
    ["signature", ["string"]],
  ],
};

/** Thrown when the gateway's answer does not meet this client's contract. */
export class ContractMismatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ContractMismatchError';
  }
}

function contractMajor(version: string): number | undefined {
  const match = /^(\d{1,4})\.(\d{1,4})$/.exec(version.trim());
  return match ? Number(match[1]) : undefined;
}

/** Throws when the gateway speaks another major contract version (no header: a gateway from before negotiation). */
export function assertCompatibleGateway(gatewayVersion: string | null | undefined): void {
  if (gatewayVersion == null) return;
  if (contractMajor(gatewayVersion) !== contractMajor(CONTRACT_VERSION)) {
    throw new ContractMismatchError(`gateway API contract ${gatewayVersion} is incompatible with this client's ${CONTRACT_VERSION}; update the app or the gateway`);
  }
}

const ROUTES = [...new Set([...Object.keys(RESPONSE_REQUIREMENTS), ...Object.keys(REQUEST_REQUIREMENTS)])];

function trimSlashes(value: string): string[] {
  return value.replace(/^\/+|\/+$/g, '').split('/');
}

/** The contract route ("PATCH /app/projects/{project_id}") that a request path matches. */
export function contractRoute(method: string, path: string): string | undefined {
  const segments = trimSlashes(path.split('?', 1)[0]);
  for (const route of ROUTES) {
    const [routeMethod, template] = route.split(' ');
    if (routeMethod !== method.toUpperCase()) continue;
    const parts = trimSlashes(template);
    const matches = parts.length === segments.length
      && parts.every((part, index) => (part.startsWith('{') && part.endsWith('}') ? segments[index] !== '' : part === segments[index]));
    if (matches) return route;
  }
  return undefined;
}

function jsonType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  return typeof value;
}

function check(value: unknown, segments: readonly string[], types: readonly string[], at: string, problems: string[]): void {
  if (segments.length === 0) {
    const actual = jsonType(value);
    if (!types.includes(actual) && !(actual === 'integer' && types.includes('number'))) {
      problems.push(`${at || 'body'}: expected ${types.join(' or ')}, got ${actual}`);
    }
    return;
  }
  // A missing, null or mistyped parent is reported by the parent's own requirement.
  if (jsonType(value) !== 'object') return;
  const [segment, ...rest] = segments;
  const each = segment.endsWith('[]');
  const name = each ? segment.slice(0, -2) : segment;
  const path = at ? `${at}.${name}` : name;
  const child = (value as Record<string, unknown>)[name];
  if (child === undefined) {
    problems.push(`${path}: missing`);
  } else if (!each) {
    check(child, rest, types, path, problems);
  } else if (Array.isArray(child)) {
    child.forEach((item, index) => check(item, rest, types, `${path}[${index}]`, problems));
  }
}

/** What `body` lacks of the fields these requirements let a client rely on. */
export function contractProblems(requirements: readonly ContractRequirement[] | undefined, body: unknown): string[] {
  const problems: string[] = [];
  for (const [path, types] of requirements || []) check(body, path ? path.split('.') : [], types, '', problems);
  return [...new Set(problems)];
}

/** Throws ContractMismatchError when a response to `method path` lacks what the contract promises. */
export function assertContractResponse(method: string, path: string, body: unknown): void {
  const route = contractRoute(method, path);
  const problems = route ? contractProblems(RESPONSE_REQUIREMENTS[route], body) : [];
  if (problems.length) throw new ContractMismatchError(`gateway response to ${route} breaks the API contract: ${problems.slice(0, 5).join('; ')}`);
}
