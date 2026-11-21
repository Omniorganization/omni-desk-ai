import { RESPONSE_REQUIREMENTS, type ContractRequirement } from '../src/generated/contract';

const SAMPLE: Record<string, unknown> = { object: {}, array: [], string: 'x', integer: 1, number: 1, boolean: false };

/** The smallest body that meets a route's contract requirements (arrays stay empty). */
export function exampleBody(route: string, requirements: readonly ContractRequirement[] = RESPONSE_REQUIREMENTS[route] || []): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const [path, types] of requirements) {
    if (!path || path.includes('[]')) continue;
    const kind = types.find(type => type !== 'null') || 'null';
    const value = kind === 'object' ? {} : kind === 'array' ? [] : SAMPLE[kind] ?? null;
    const segments = path.split('.');
    let target = body;
    for (const segment of segments.slice(0, -1)) target = target[segment] as Record<string, unknown>;
    target[segments[segments.length - 1]] = value;
  }
  return body;
}
