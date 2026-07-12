import 'dart:async';

import 'omni_api.dart';

class ChatStreamRunResult {
  const ChatStreamRunResult({
    required this.streamed,
    required this.lastEventId,
    required this.completed,
    required this.cancelled,
    this.fallbackResult,
  });

  final bool streamed;
  final int lastEventId;
  final bool completed;
  final bool cancelled;
  final Map<String, dynamic>? fallbackResult;
}

class MobileChatStreamSession {
  MobileChatStreamSession._({required Future<void> Function() runner}) {
    scheduleMicrotask(() async {
      try {
        await runner();
      } catch (error, stackTrace) {
        if (!_resultCompleter.isCompleted) {
          _resultCompleter.completeError(error, stackTrace);
        }
        if (!_events.isClosed) _events.addError(error, stackTrace);
      } finally {
        if (!_events.isClosed) await _events.close();
      }
    });
  }

  final StreamController<ChatStreamEvent> _events =
      StreamController<ChatStreamEvent>.broadcast();
  final Completer<ChatStreamRunResult> _resultCompleter =
      Completer<ChatStreamRunResult>();
  StreamSubscription<ChatStreamEvent>? _activeSubscription;
  bool _cancelled = false;
  int _lastEventId = 0;
  bool _observed = false;

  Stream<ChatStreamEvent> get events => _events.stream;
  Future<ChatStreamRunResult> get result => _resultCompleter.future;
  bool get isCancelled => _cancelled;

  void attach(StreamSubscription<ChatStreamEvent> subscription) {
    _activeSubscription = subscription;
    if (_cancelled) unawaited(subscription.cancel());
  }

  void emit(ChatStreamEvent event) {
    _observed = true;
    _lastEventId = event.id;
    if (!_cancelled && !_events.isClosed) _events.add(event);
  }

  void complete(ChatStreamRunResult value) {
    if (!_resultCompleter.isCompleted) _resultCompleter.complete(value);
  }

  Future<void> cancel() async {
    if (_cancelled) return;
    _cancelled = true;
    await _activeSubscription?.cancel();
    complete(
      ChatStreamRunResult(
        streamed: _observed,
        lastEventId: _lastEventId,
        completed: false,
        cancelled: true,
      ),
    );
  }
}

extension OmniStreamingController on OmniApiClient {
  MobileChatStreamSession startChatStreamSession({
    required String conversationId,
    required String content,
    String modelProfile = 'fast',
    String? sourceDeviceId,
    String? idempotencyKey,
    int lastEventId = 0,
    int maxReconnects = 1,
  }) {
    late MobileChatStreamSession session;
    session = MobileChatStreamSession._(runner: () async {
      var observed = false;
      var completed = false;
      var cursor = lastEventId;
      var reconnects = 0;
      while (!session.isCancelled) {
        final streamDone = Completer<void>();
        Object? streamError;
        StackTrace? streamStack;
        final subscription = streamChat(
          conversationId: conversationId,
          content: content,
          modelProfile: modelProfile,
          sourceDeviceId: sourceDeviceId,
          idempotencyKey: idempotencyKey,
          lastEventId: cursor,
        ).listen(
          (event) {
            observed = true;
            cursor = event.id;
            completed = event.event == 'chat.completed' || completed;
            session.emit(event);
          },
          onError: (Object error, StackTrace stackTrace) {
            streamError = error;
            streamStack = stackTrace;
            if (!streamDone.isCompleted) streamDone.complete();
          },
          onDone: () {
            if (!streamDone.isCompleted) streamDone.complete();
          },
          cancelOnError: true,
        );
        session.attach(subscription);
        await streamDone.future;
        if (session.isCancelled) {
          session.complete(
            ChatStreamRunResult(
              streamed: observed,
              lastEventId: cursor,
              completed: false,
              cancelled: true,
            ),
          );
          return;
        }
        if (streamError == null) {
          session.complete(
            ChatStreamRunResult(
              streamed: true,
              lastEventId: cursor,
              completed: completed,
              cancelled: false,
            ),
          );
          return;
        }
        if (observed && !completed && reconnects < maxReconnects) {
          reconnects += 1;
          continue;
        }
        if (observed) {
          Error.throwWithStackTrace(streamError!, streamStack ?? StackTrace.current);
        }
        final fallback = await askConversation(
          conversationId,
          content,
          modelProfile: modelProfile,
          sourceDeviceId: sourceDeviceId,
          idempotencyKey: idempotencyKey,
        );
        session.complete(
          ChatStreamRunResult(
            streamed: false,
            lastEventId: cursor,
            completed: true,
            cancelled: false,
            fallbackResult: fallback,
          ),
        );
        return;
      }
    });
    return session;
  }

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
    final session = startChatStreamSession(
      conversationId: conversationId,
      content: content,
      modelProfile: modelProfile,
      sourceDeviceId: sourceDeviceId,
      idempotencyKey: idempotencyKey,
      lastEventId: lastEventId,
      maxReconnects: maxReconnects,
    );
    final subscription = session.events.listen(onEvent);
    try {
      return await session.result;
    } finally {
      await subscription.cancel();
    }
  }
}
