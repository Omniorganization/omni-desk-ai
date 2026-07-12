import 'dart:async';

import 'omni_api.dart';

class ChatStreamRunResult {
  const ChatStreamRunResult({
    required this.streamed,
    required this.lastEventId,
    required this.completed,
    this.fallbackResult,
  });

  final bool streamed;
  final int lastEventId;
  final bool completed;
  final Map<String, dynamic>? fallbackResult;
}

extension OmniStreamingController on OmniApiClient {
  Future<ChatStreamRunResult> streamChatWithFallback({
    required String conversationId,
    required String content,
    String modelProfile = 'fast',
    String? sourceDeviceId,
    String? idempotencyKey,
    int lastEventId = 0,
    int maxReconnects = 1,
    required void Function(ChatStreamEvent event) onEvent,
  }) async {
    var observed = false;
    var completed = false;
    var cursor = lastEventId;
    var reconnects = 0;
    while (true) {
      try {
        await for (final event in streamChat(
          conversationId: conversationId,
          content: content,
          modelProfile: modelProfile,
          sourceDeviceId: sourceDeviceId,
          idempotencyKey: idempotencyKey,
          lastEventId: cursor,
        )) {
          observed = true;
          cursor = event.id;
          completed = event.event == 'chat.completed' || completed;
          onEvent(event);
        }
        return ChatStreamRunResult(
          streamed: true,
          lastEventId: cursor,
          completed: completed,
        );
      } catch (_) {
        if (observed && !completed && reconnects < maxReconnects) {
          reconnects += 1;
          continue;
        }
        if (observed) rethrow;
        final fallback = await askConversation(
          conversationId,
          content,
          modelProfile: modelProfile,
          sourceDeviceId: sourceDeviceId,
          idempotencyKey: idempotencyKey,
        );
        return ChatStreamRunResult(
          streamed: false,
          lastEventId: cursor,
          completed: true,
          fallbackResult: fallback,
        );
      }
    }
  }
}
