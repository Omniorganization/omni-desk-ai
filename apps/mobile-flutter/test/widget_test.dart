import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/main.dart';

void main() {
  testWidgets('renders Omni Mobile Approval', (WidgetTester tester) async {
    await tester.pumpWidget(const OmniMobileApp());
    expect(find.text('Omni Mobile Approval'), findsOneWidget);
  });
}
