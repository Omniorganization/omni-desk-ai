import 'dart:async';

import 'package:firebase_messaging/firebase_messaging.dart';

/// What a gateway push asks the app to show.
class PushRoute {
  const PushRoute({
    required this.eventType,
    this.relatedId,
    this.title,
    this.body,
  });

  /// The gateway notification's event type, for example `approval.requested`.
  final String eventType;

  /// The approval, task or conversation the push is about.
  final String? relatedId;
  final String? title;
  final String? body;

  bool get isApprovalRequest => eventType == 'approval.requested';
}

/// Reads the routing data the gateway puts in every push (`push_id`,
/// `event_type`, `related_id`). Returns null for a message from anywhere else.
PushRoute? routeForPush(RemoteMessage message) {
  final data = message.data;
  if ('${data['push_id'] ?? ''}'.isEmpty) return null;
  final relatedId = '${data['related_id'] ?? ''}';
  return PushRoute(
    eventType: '${data['event_type'] ?? ''}',
    relatedId: relatedId.isEmpty ? null : relatedId,
    title: message.notification?.title,
    body: message.notification?.body,
  );
}

/// Registered with FirebaseMessaging.onBackgroundMessage. Gateway pushes are
/// notification messages: while the app is in the background or terminated
/// the system shows them itself, and the app refreshes from the gateway when
/// the user opens one, so the background isolate has no work to do.
@pragma('vm:entry-point')
Future<void> omniPushBackgroundHandler(RemoteMessage message) async {}

/// Delivers gateway pushes to the app: `onForeground` while it is open, and
/// `onOpened` when the user taps a notification, including the one that
/// launched the app.
class OmniPushMessages {
  OmniPushMessages({
    Stream<RemoteMessage>? foreground,
    Stream<RemoteMessage>? opened,
    Future<RemoteMessage?> Function()? initialMessage,
  }) : _foreground = foreground ?? FirebaseMessaging.onMessage,
       _opened = opened ?? FirebaseMessaging.onMessageOpenedApp,
       _initialMessage =
           initialMessage ?? FirebaseMessaging.instance.getInitialMessage;

  final Stream<RemoteMessage> _foreground;
  final Stream<RemoteMessage> _opened;
  final Future<RemoteMessage?> Function() _initialMessage;
  final List<StreamSubscription<RemoteMessage>> _subscriptions =
      <StreamSubscription<RemoteMessage>>[];

  Future<void> listen({
    required void Function(PushRoute route) onForeground,
    required void Function(PushRoute route) onOpened,
  }) async {
    void deliver(RemoteMessage message, void Function(PushRoute route) to) {
      final route = routeForPush(message);
      if (route != null) to(route);
    }

    _subscriptions
      ..add(_foreground.listen((message) => deliver(message, onForeground)))
      ..add(_opened.listen((message) => deliver(message, onOpened)));
    final initial = await _initialMessage();
    if (initial != null) deliver(initial, onOpened);
  }

  Future<void> dispose() async {
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    _subscriptions.clear();
  }
}
