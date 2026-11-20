// Generated from apps/shared/omni-app-api.contract.json by scripts/generate_contract_clients.py. Do not edit; run the script.

const String contractVersion = "1.0";
const String contractHeader = 'x-omnidesk-contract';

/// A field path the client relies on (`a[].b`: field b of every element of
/// array a) and the JSON types it may have.
class ContractRequirement {
  const ContractRequirement(this.path, this.types);

  final String path;
  final List<String> types;
}

const Map<String, List<ContractRequirement>> responseRequirements = <String, List<ContractRequirement>>{
  "GET /app/bootstrap": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("pending_approvals", <String>["array"]),
    ContractRequirement("pending_approvals[]", <String>["object"]),
    ContractRequirement("pending_approvals[].approval_id", <String>["string"]),
    ContractRequirement("pending_approvals[].action", <String>["string"]),
    ContractRequirement("pending_approvals[].risk", <String>["string"]),
    ContractRequirement("notifications", <String>["array"]),
    ContractRequirement("notifications[]", <String>["object"]),
    ContractRequirement("notifications[].notification_id", <String>["string"]),
    ContractRequirement("notifications[].title", <String>["string"]),
    ContractRequirement("notifications[].body", <String>["string"]),
    ContractRequirement("runtime_status", <String>["array"]),
    ContractRequirement("devices", <String>["array"]),
  ],
  "GET /app/projects": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("projects", <String>["array"]),
    ContractRequirement("projects[]", <String>["object"]),
    ContractRequirement("projects[].project_id", <String>["string"]),
    ContractRequirement("projects[].name", <String>["string"]),
    ContractRequirement("projects[].archived", <String>["boolean"]),
  ],
  "POST /app/projects": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("project", <String>["object"]),
    ContractRequirement("project.project_id", <String>["string"]),
    ContractRequirement("project.name", <String>["string"]),
    ContractRequirement("project.archived", <String>["boolean"]),
  ],
  "PATCH /app/projects/{project_id}": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "DELETE /app/projects/{project_id}": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/register": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device", <String>["object"]),
    ContractRequirement("device.device_id", <String>["string"]),
    ContractRequirement("device.credential_status", <String>["string"]),
  ],
  "GET /app/conversations": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("conversations", <String>["array"]),
  ],
  "POST /app/conversations": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("conversation", <String>["object"]),
    ContractRequirement("conversation.conversation_id", <String>["string"]),
  ],
  "POST /app/conversations/{conversation_id}/messages": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "GET /app/conversations/{conversation_id}/messages": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("messages", <String>["array"]),
    ContractRequirement("messages[]", <String>["object"]),
    ContractRequirement("messages[].role", <String>["string"]),
    ContractRequirement("messages[].content", <String>["string"]),
  ],
  "POST /app/conversations/{conversation_id}/ask": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("assistant_message", <String>["object"]),
    ContractRequirement("assistant_message.role", <String>["string"]),
    ContractRequirement("assistant_message.content", <String>["string"]),
  ],
  "POST /app/tasks/{task_id}/status": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "GET /app/approvals": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("approvals", <String>["array"]),
  ],
  "POST /app/approvals/{approval_id}/decide": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "GET /app/notifications": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("notifications", <String>["array"]),
    ContractRequirement("notifications[]", <String>["object"]),
    ContractRequirement("notifications[].notification_id", <String>["string"]),
    ContractRequirement("notifications[].title", <String>["string"]),
    ContractRequirement("notifications[].body", <String>["string"]),
  ],
  "POST /app/runtime/desktop/heartbeat": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/runtime/desktop/claim": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("task", <String>["object", "null"]),
    ContractRequirement("task.task_id", <String>["string"]),
    ContractRequirement("task.title", <String>["string"]),
    ContractRequirement("task.action", <String>["object"]),
  ],
  "GET /app/sync": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/{device_id}/push-token": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/{device_id}/approval-key": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/enrollment/start": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("enrollment", <String>["object"]),
    ContractRequirement("enrollment.enrollment_id", <String>["string"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/complete": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/challenge": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("challenge", <String>["object"]),
    ContractRequirement("challenge.challenge_id", <String>["string"]),
    ContractRequirement("challenge.signing_message", <String>["string"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/verify": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("credential", <String>["object"]),
    ContractRequirement("credential.trust_level", <String>["string"]),
  ],
  "GET /admin/session/identity": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("actor", <String>["string"]),
    ContractRequirement("role", <String>["string"]),
  ],
  "GET /admin/channels/ecosystem": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("channels", <String>["array"]),
  ],
};

const Map<String, List<ContractRequirement>> requestRequirements = <String, List<ContractRequirement>>{
  "POST /app/projects": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("name", <String>["string"]),
  ],
  "PATCH /app/projects/{project_id}": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/devices/register": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device_id", <String>["string"]),
    ContractRequirement("device_type", <String>["string"]),
  ],
  "POST /app/conversations": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
  ],
  "POST /app/conversations/{conversation_id}/messages": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("content", <String>["string"]),
  ],
  "POST /app/conversations/{conversation_id}/ask": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("content", <String>["string"]),
  ],
  "POST /api/chat/stream": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("content", <String>["string"]),
  ],
  "POST /app/tasks/{task_id}/status": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("status", <String>["string"]),
  ],
  "POST /app/approvals/{approval_id}/decide": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("decision", <String>["string"]),
  ],
  "POST /app/runtime/desktop/heartbeat": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device_id", <String>["string"]),
    ContractRequirement("status", <String>["string"]),
  ],
  "POST /app/runtime/desktop/claim": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device_id", <String>["string"]),
  ],
  "POST /app/devices/{device_id}/push-token": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("push_token", <String>["string"]),
  ],
  "POST /app/devices/{device_id}/approval-key": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("public_key", <String>["string"]),
  ],
  "POST /app/devices/enrollment/start": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device_type", <String>["string"]),
    ContractRequirement("pairing_code", <String>["string"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/complete": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("pairing_code", <String>["string"]),
    ContractRequirement("device_id", <String>["string"]),
    ContractRequirement("public_key", <String>["string"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/challenge": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("device_id", <String>["string"]),
  ],
  "POST /app/devices/enrollment/{enrollment_id}/verify": <ContractRequirement>[
    ContractRequirement("", <String>["object"]),
    ContractRequirement("challenge_id", <String>["string"]),
    ContractRequirement("device_id", <String>["string"]),
    ContractRequirement("signature", <String>["string"]),
  ],
};

/// Thrown when the gateway's answer does not meet this client's contract.
class ContractMismatchException implements Exception {
  const ContractMismatchException(this.message);

  final String message;

  @override
  String toString() => 'ContractMismatchException: $message';
}

int? _contractMajor(String version) {
  final match = RegExp(r'^(\d{1,4})\.(\d{1,4})$').firstMatch(version.trim());
  return match == null ? null : int.parse(match.group(1)!);
}

/// Throws when the gateway speaks another major contract version (no header:
/// a gateway from before negotiation).
void assertCompatibleGateway(String? gatewayVersion) {
  if (gatewayVersion == null) return;
  if (_contractMajor(gatewayVersion) != _contractMajor(contractVersion)) {
    throw ContractMismatchException(
      "gateway API contract $gatewayVersion is incompatible with this client's $contractVersion; update the app or the gateway",
    );
  }
}

List<String> _trimSlashes(String value) =>
    value.replaceAll(RegExp(r'^/+|/+$'), '').split('/');

/// The contract route ("PATCH /app/projects/{project_id}") a request path matches.
String? contractRoute(String method, String path) {
  final segments = _trimSlashes(path.split('?').first);
  final routes = <String>{
    ...responseRequirements.keys,
    ...requestRequirements.keys,
  };
  for (final route in routes) {
    final separator = route.indexOf(' ');
    if (route.substring(0, separator) != method.toUpperCase()) continue;
    final parts = _trimSlashes(route.substring(separator + 1));
    if (parts.length != segments.length) continue;
    var matches = true;
    for (var i = 0; i < parts.length; i++) {
      final templated = parts[i].startsWith('{') && parts[i].endsWith('}');
      if (templated ? segments[i].isEmpty : parts[i] != segments[i]) {
        matches = false;
        break;
      }
    }
    if (matches) return route;
  }
  return null;
}

String _jsonType(Object? value) {
  if (value == null) return 'null';
  if (value is List) return 'array';
  if (value is Map) return 'object';
  if (value is int) return 'integer';
  if (value is num) return 'number';
  if (value is String) return 'string';
  if (value is bool) return 'boolean';
  return 'unknown';
}

void _check(
  Object? value,
  List<String> segments,
  List<String> types,
  String at,
  Set<String> problems,
) {
  if (segments.isEmpty) {
    final actual = _jsonType(value);
    if (!types.contains(actual) &&
        !(actual == 'integer' && types.contains('number'))) {
      problems.add(
        '${at.isEmpty ? 'body' : at}: expected ${types.join(' or ')}, got $actual',
      );
    }
    return;
  }
  // A missing, null or mistyped parent is reported by the parent's own requirement.
  if (value is! Map) return;
  final segment = segments.first;
  final each = segment.endsWith('[]');
  final name = each ? segment.substring(0, segment.length - 2) : segment;
  final path = at.isEmpty ? name : '$at.$name';
  if (!value.containsKey(name)) {
    problems.add('$path: missing');
    return;
  }
  final child = value[name];
  final rest = segments.sublist(1);
  if (!each) {
    _check(child, rest, types, path, problems);
  } else if (child is List) {
    for (var i = 0; i < child.length; i++) {
      _check(child[i], rest, types, '$path[$i]', problems);
    }
  }
}

/// What [body] lacks of the fields these requirements let a client rely on.
List<String> contractProblems(
  List<ContractRequirement>? requirements,
  Object? body,
) {
  final problems = <String>{};
  for (final requirement in requirements ?? const <ContractRequirement>[]) {
    _check(
      body,
      requirement.path.isEmpty ? const <String>[] : requirement.path.split('.'),
      requirement.types,
      '',
      problems,
    );
  }
  return problems.toList();
}

/// Throws when a response to [method] [path] lacks what the contract promises.
void assertContractResponse(String method, String path, Object? body) {
  final route = contractRoute(method, path);
  if (route == null) return;
  final problems = contractProblems(responseRequirements[route], body);
  if (problems.isNotEmpty) {
    throw ContractMismatchException(
      'gateway response to $route breaks the API contract: ${problems.take(5).join('; ')}',
    );
  }
}
