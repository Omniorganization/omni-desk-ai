import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/main.dart';

void main() {
  testWidgets('renders Codex-style Omni Mobile assistant shell', (WidgetTester tester) async {
    FlutterSecureStorage.setMockInitialValues(<String, String>{});

    await tester.pumpWidget(const OmniMobileApp());
    await tester.pump();

    expect(find.byType(OmniMobileApp), findsOneWidget);
  });
}
