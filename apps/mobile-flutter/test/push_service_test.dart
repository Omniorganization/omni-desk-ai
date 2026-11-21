import 'dart:async';

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/push_service.dart';

// The data every gateway push carries (omnidesk_agent/appsync/push.py).
RemoteMessage _gatewayPush({
  String eventType = 'approval.requested',
  String relatedId = 'appr_1',
}) => RemoteMessage(
  data: <String, dynamic>{
    'push_id': 'push_1',
    'event_type': eventType,
    'related_id': relatedId,
    'audience': 'mobile',
  },
  notification: RemoteNotification(
    title: 'Omni approval required',
    body: 'Deploy the release',
  ),
);

Future<void> _drain() async {
  for (var i = 0; i < 3; i++) {
    await Future<void>.delayed(Duration.zero);
  }
}

void main() {
  test('a gateway push routes to the approval it is about', () {
    final route = routeForPush(_gatewayPush())!;
    expect(route.isApprovalRequest, isTrue);
    expect(route.relatedId, 'appr_1');
    expect(route.title, 'Omni approval required');
    expect(route.body, 'Deploy the release');
  });

  test('a task update is not an approval request', () {
    final route = routeForPush(
      _gatewayPush(eventType: 'task.status', relatedId: 'task_1'),
    )!;
    expect(route.isApprovalRequest, isFalse);
    expect(route.eventType, 'task.status');
  });

  test('messages that are not from the gateway are ignored', () {
    expect(
      routeForPush(RemoteMessage(data: <String, dynamic>{'campaign': 'x'})),
      isNull,
    );
  });

  test(
    'foreground, tapped and launching pushes reach the app until disposed',
    () async {
      final foreground = StreamController<RemoteMessage>.broadcast();
      final opened = StreamController<RemoteMessage>.broadcast();
      final seen = <String>[];
      final messages = OmniPushMessages(
        foreground: foreground.stream,
        opened: opened.stream,
        initialMessage: () async => _gatewayPush(relatedId: 'appr_launch'),
      );
      await messages.listen(
        onForeground: (route) => seen.add('foreground:${route.relatedId}'),
        onOpened: (route) => seen.add('opened:${route.relatedId}'),
      );
      foreground.add(_gatewayPush(relatedId: 'appr_live'));
      foreground.add(RemoteMessage(data: <String, dynamic>{'campaign': 'x'}));
      opened.add(_gatewayPush(relatedId: 'appr_tapped'));
      await _drain();
      expect(seen, <String>[
        'opened:appr_launch',
        'foreground:appr_live',
        'opened:appr_tapped',
      ]);

      await messages.dispose();
      foreground.add(_gatewayPush(relatedId: 'appr_after_dispose'));
      await _drain();
      expect(seen, hasLength(3));
      await foreground.close();
      await opened.close();
    },
  );
}
