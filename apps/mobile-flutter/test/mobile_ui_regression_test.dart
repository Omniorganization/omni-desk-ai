import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/main.dart';

void main() {
  const storageChannel = MethodChannel(
    'plugins.it_nomads.com/flutter_secure_storage',
  );
  final binding = TestWidgetsFlutterBinding.ensureInitialized();
  var storedValues = <String, String>{};
  var writes = <String>[];
  var denyTokenRead = false;

  setUp(() {
    storedValues = <String, String>{};
    writes = <String>[];
    denyTokenRead = false;
    binding.defaultBinaryMessenger.setMockMethodCallHandler(storageChannel, (
      call,
    ) async {
      final arguments = Map<String, dynamic>.from(call.arguments as Map);
      final key = arguments['key'] as String;
      if (call.method == 'read') {
        if (denyTokenRead && key == 'omni.token') {
          throw PlatformException(
            code: '-34018',
            message: 'native credential detail',
          );
        }
        return storedValues[key];
      }
      if (call.method == 'write') {
        writes.add(key);
        storedValues[key] = arguments['value'] as String;
        return null;
      }
      throw MissingPluginException('Unexpected secure storage operation');
    });
  });

  tearDown(() {
    binding.defaultBinaryMessenger.setMockMethodCallHandler(
      storageChannel,
      null,
    );
  });

  testWidgets('unenrolled sync reports a visible error without creating keys', (
    tester,
  ) async {
    await tester.pumpWidget(const OmniMobileApp());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('同步'));
    await tester.tap(find.text('同步'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.textContaining('请先连接 Omni Gateway 完成设备登记。'), findsOneWidget);
    expect(writes, isEmpty);
    expect(storedValues.containsKey('omni.device_private_key.v2'), isFalse);
  });

  testWidgets(
    'secure storage read failure stays visible and restores no token',
    (tester) async {
      storedValues['omni.gateway'] = 'https://unrestored.example.test';
      storedValues['omni.token'] = 'must-not-be-restored';
      denyTokenRead = true;
      await tester.pumpWidget(const OmniMobileApp());
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.text('Security: secure storage unavailable'), findsOneWidget);
      final restoreError = find.text('无法恢复会话：安全存储不可用，请检查应用签名与设备权限。');
      await tester.scrollUntilVisible(
        restoreError,
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(restoreError, findsOneWidget);
      await tester.scrollUntilVisible(
        find.text('连接配置'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.text('连接配置'));
      await tester.pumpAndSettle();
      final token = tester.widget<TextField>(
        find.byWidgetPredicate(
          (widget) =>
              widget is TextField &&
              widget.decoration?.labelText == 'Owner/Operator Token',
        ),
      );
      final gateway = tester.widget<TextField>(
        find.byWidgetPredicate(
          (widget) =>
              widget is TextField &&
              widget.decoration?.labelText == 'Gateway URL',
        ),
      );
      expect(token.controller!.text, isEmpty);
      expect(gateway.controller!.text, 'http://127.0.0.1:18789');
      expect(find.textContaining('native credential detail'), findsNothing);
      expect(writes, isEmpty);
    },
  );

  testWidgets('headline and section titles use the app dark theme', (
    tester,
  ) async {
    await tester.pumpWidget(const OmniMobileApp());
    await tester.pumpAndSettle();
    final headline = find.text('我们应该在 AI 助理中做些什么？');
    final headlineText = tester.widget<Text>(headline);
    final headlineTheme = Theme.of(tester.element(headline));
    expect(headlineTheme.brightness, Brightness.dark);
    expect(
      headlineText.style!.color,
      headlineTheme.textTheme.headlineSmall!.color,
    );

    for (final label in <String>['待审批：0', '通知：0']) {
      final title = find.text(label);
      await tester.scrollUntilVisible(
        title,
        300,
        scrollable: find.byType(Scrollable).first,
      );
      final text = tester.widget<Text>(title);
      expect(
        text.style!.color,
        Theme.of(tester.element(title)).textTheme.titleLarge!.color,
      );
    }
    expect(tester.takeException(), isNull);
  });
}
