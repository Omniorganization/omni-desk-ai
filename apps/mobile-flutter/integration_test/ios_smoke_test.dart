import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:omnidesk_mobile/main.dart' as app;

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('native Keychain works and offline sync reports enrollment', (
    tester,
  ) async {
    const storage = FlutterSecureStorage();
    const key = 'omni.ios-smoke.keychain';
    try {
      await storage.write(key: key, value: 'native-readback');
      expect(await storage.read(key: key), 'native-readback');
      await storage.delete(key: key);
      expect(await storage.read(key: key), isNull);

      await app.main();
      final ready = find.text('Security: secure storage ready');
      final unavailable = find.text('Security: secure storage unavailable');
      final deadline = tester.binding.clock.fromNowBy(
        const Duration(seconds: 30),
      );
      // Native Keychain reads can remain pending after animation frames settle.
      while (ready.evaluate().isEmpty &&
          unavailable.evaluate().isEmpty &&
          tester.binding.clock.now().isBefore(deadline)) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      expect(
        ready,
        findsOneWidget,
        reason:
            'Native session restore must finish successfully; security labels: '
            '${tester.widgetList<Text>(find.textContaining('Security: ')).map((widget) => widget.data).toList()}',
      );
      final headline = find.text('我们应该在 AI 助理中做些什么？');
      final style = tester.widget<Text>(headline).style!;
      expect(
        style.color,
        Theme.of(tester.element(headline)).textTheme.headlineSmall!.color,
      );
      await tester.ensureVisible(find.text('同步'));
      await tester.tap(find.text('同步'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.textContaining('请先连接 Omni Gateway 完成设备登记。'), findsOneWidget);
    } finally {
      await storage.delete(key: key);
    }
  });
}
