import 'package:flutter_test/flutter_test.dart';
import 'package:pistachio_app/main.dart';

void main() {
  testWidgets('Pistachio app smoke test', (WidgetTester tester) async {
    await tester.pumpWidget(const PistachioApp());
    expect(find.text('Pistachio'), findsOneWidget);
  });
}
