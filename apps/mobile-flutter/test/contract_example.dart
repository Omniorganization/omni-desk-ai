import 'package:omnidesk_mobile/generated/contract.dart';

/// The smallest body that meets a route's contract requirements (arrays stay
/// empty), for fake gateway answers.
Map<String, dynamic> exampleBody(String route) {
  final body = <String, dynamic>{};
  for (final requirement in responseRequirements[route] ?? const <ContractRequirement>[]) {
    final path = requirement.path;
    if (path.isEmpty || path.contains('[]')) continue;
    final kind = requirement.types.firstWhere(
      (type) => type != 'null',
      orElse: () => 'null',
    );
    final Object? value = switch (kind) {
      'object' => <String, dynamic>{},
      'array' => <dynamic>[],
      'string' => 'x',
      'integer' || 'number' => 1,
      'boolean' => false,
      _ => null,
    };
    final segments = path.split('.');
    var target = body;
    for (final segment in segments.sublist(0, segments.length - 1)) {
      target = target[segment] as Map<String, dynamic>;
    }
    target[segments.last] = value;
  }
  return body;
}
