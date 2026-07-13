export type ChatStreamEventName =
  | 'chat.started'
  | 'chat.delta'
  | 'chat.reasoning.delta'
  | 'chat.usage'
  | 'chat.completed'
  | 'chat.failed';

export interface ChatStreamEvent {
  id: number;
  event: ChatStreamEventName;
  data: Record<string, unknown>;
}

export function parseSseBlock(block: string): ChatStreamEvent | null {
  let id = 0;
  let event = '';
  const data: string[] = [];
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'id') id = Number(value);
    else if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (!event || !Number.isSafeInteger(id) || id < 1) return null;
  try {
    const decoded = JSON.parse(data.join('\n')) as unknown;
    return {
      id,
      event: event as ChatStreamEventName,
      data: decoded && typeof decoded === 'object' && !Array.isArray(decoded)
        ? decoded as Record<string, unknown>
        : {},
    };
  } catch {
    return {
      id,
      event: event as ChatStreamEventName,
      data: { code: 'invalid_stream_event' },
    };
  }
}

export async function consumeSse(
  response: Response,
  options: {
    signal?: AbortSignal;
    lastEventId?: number;
    onEvent: (event: ChatStreamEvent) => void;
  },
): Promise<{ lastEventId: number; completed: boolean }> {
  if (!response.body) throw new Error('stream_body_unavailable');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lastEventId = options.lastEventId || 0;
  let completed = false;
  try {
    while (true) {
      if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const { value, done } = await reader.read();
      buffer += decoder
        .decode(value || new Uint8Array(), { stream: !done })
        .replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const event = parseSseBlock(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (event && event.id > lastEventId) {
          lastEventId = event.id;
          options.onEvent(event);
          if (event.event === 'chat.completed') completed = true;
          if (event.event === 'chat.failed') {
            throw new Error(String(event.data.code || 'chat_stream_failed'));
          }
        }
        boundary = buffer.indexOf('\n\n');
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
  return { lastEventId, completed };
}
