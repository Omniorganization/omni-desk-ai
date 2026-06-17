import 'package:flutter_test/flutter_test.dart';
import 'package:omnidesk_mobile/main.dart';

void main() {
  testWidgets('renders OmniDesk AI dashboard', (WidgetTester tester) async {
    await tester.pumpWidget(const OmniMobileApp());
    await tester.pumpAndSettle();

    expect(find.text('OmniDesk AI'), findsOneWidget);
    expect(find.text('Hello, OmniDesk'), findsOneWidget);
    expect(find.text('12'), findsOneWidget);
    expect(find.text('3'), findsOneWidget);
    expect(find.text('98%'), findsOneWidget);
    expect(find.text('帮我分析销售数据'), findsOneWidget);
  });

  testWidgets('opens analysis detail and report actions',
      (WidgetTester tester) async {
    await tester.pumpWidget(const OmniMobileApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('帮我分析销售数据'));
    await tester.pumpAndSettle();

    expect(find.text('销售数据分析报告'), findsOneWidget);
    expect(find.text('已完成分析'), findsOneWidget);

    await tester.tap(find.text('导出'));
    await tester.pumpAndSettle();

    expect(find.text('已导出销售分析报告草稿'), findsOneWidget);
  });
}
