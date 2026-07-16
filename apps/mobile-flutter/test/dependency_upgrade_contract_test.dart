import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('Flutter 3.38 dependency upgrade remains evidenced and native-compatible', () {
    final pubspec = File('pubspec.yaml').readAsStringSync();
    final mainSource = File('lib/main.dart').readAsStringSync();
    final podfile = File('ios/Podfile').readAsStringSync();
    final workflow = File('../../.github/workflows/tri-app-quality.yml')
        .readAsStringSync();
    final evidence = jsonDecode(
      File('evidence/flutter-3.38-upgrade.json').readAsStringSync(),
    ) as Map<String, dynamic>;

    expect(pubspec, contains('local_auth: ^3.0.2'));
    expect(pubspec, contains('firebase_core: ^4.12.1'));
    expect(pubspec, contains('firebase_messaging: ^16.4.3'));
    expect(mainSource, isNot(contains('AuthenticationOptions(')));
    expect(mainSource, contains('persistAcrossBackgrounding: true'));
    expect(podfile, contains("platform :ios, '15.0'"));
    expect(
      podfile,
      contains("IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'"),
    );
    expect(workflow, contains('flutter-version: "3.38.x"'));
    expect(evidence['schema'], 'omnidesk-dependency-upgrade-evidence/v1');
    expect((evidence['tests'] as List<dynamic>), contains('flutter test'));
    expect((evidence['risk_notes'] as List<dynamic>).length, greaterThanOrEqualTo(2));
    expect((evidence['rollback_steps'] as List<dynamic>).length, greaterThanOrEqualTo(2));
  });
}
