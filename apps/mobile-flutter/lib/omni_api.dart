import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'device_identity.dart';

class ChatStreamEvent {
  const ChatStreamEvent({
    required this.id,
    required this.event,
    required this.data,
  });

  final int id;
  final String event;
  final Map<String, dynamic> data;
}

class OmniApiClient {
  OmniApiClient({
    required String baseUrl,
    required this.token,
    required this.actor,
    http.Client? httpClient,
    DeviceIdentityStore? deviceIdentityStore,
  })  : baseUrl = _normalizeBaseUrl(baseUrl),
        _httpClient = httpClient ?? http.Client(),
        _deviceIdentityStore = deviceIdentityStore;

  final String baseUrl;
  final String token;
  final String actor;
  final http.Client _httpClient;
  final DeviceIdentityStore? _deviceIdentityStore;

  Future<Map<String, String>> _headers(
    String method,
    String path,
    String body, [
    String? idempotencyKey,
  ]) async {
    final signedHeaders = _deviceIdentityStore == null
        ? <String, String>{}
        : await _deviceIdentityStore.signRequest(
            method: method,
            path: path,
            body: body,
          );
    return <String, String>{
      'content-type': 'application/json',
      'authorization': 'Bearer $token',
      'x-omnidesk-actor': actor,
      if (idempotencyKey != null && idempotencyKey.isNotEmpty)
        'idempotency-key': idempotencyKey,
      ...signedHeaders,
    };
  }

  Future<Map<String, dynamic>> _request(
    String method,
    String path, [
    Map<String, dynamic>? body,
    String? idempotencyKey,
    Map<String, String> extraHeaders = const <String, String>{},
  ]) async {
    final uri = Uri.parse('${baseUrl.replaceAll(RegExp(r'/$'), '')}$path');
    late http.Response response;
    final requestBody = method == 'GET' || method == 'DELETE'
        ? ''
        : jsonEncode(body ?? <String, dynamic>{});
    final headers = <String, String>{
      ...await _headers(method, path, requestBody, idempotencyKey),
      ...extraHeaders,
    };
    if (method == 'GET') {
      response = await _httpClient.get(uri, headers: headers);
    } else if (method == 'PATCH') {
      response = await _httpClient.patch(uri, headers: headers, body: requestBody);
    } else if (method == 'DELETE') {
      response = await _httpClient.delete(uri, headers: headers);
    } else {
      response = await _httpClient.post(
        uri,
        headers: headers,
        body: requestBody,
      );
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(_safeApiError(response));
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> bootstrap() =>
      _request('GET', '/app/bootstrap');

  Future<Map<String, dynamic>> projects() => _request('GET', '/app/projects');

  Future<Map<String, dynamic>> createProject(
    String name, {
    String description = '',
    Map<String, dynamic> metadata = const <String, dynamic>{},
    String? sourceDeviceId,
    String? idempotencyKey,
  }) =>
      _request(
        'POST',
        '/app/projects',
        <String, dynamic>{
          'name': name,
          'description': description,
          'metadata': metadata,
          if (sourceDeviceId != null && sourceDeviceId.isNotEmpty)
            'source_device_id': sourceDeviceId,
        },
        idempotencyKey ??
            'mobile-project-create-${name.hashCode}-${DateTime.now().millisecondsSinceEpoch}',
      );

  Future<Map<String, dynamic>> updateProject(
    String projectId, {
    String? name,
    String? description,
    Map<String, dynamic>? metadata,
    bool? archived,
    String? idempotencyKey,
  }) =>
      _request(
        'PATCH',
        '/app/projects/${_pathSegment(projectId)}',
        <String, dynamic>{
          if (name != null) 'name': name,
          if (description != null) 'description': description,
          if (metadata != null) 'metadata': metadata,
          if (archived != null) 'archived': archived,
        },
        idempotencyKey ??
            'mobile-project-update-$projectId-${DateTime.now().millisecondsSinceEpoch}',
      );

  Future<Map<String, dynamic>> deleteProject(
    String projectId, {
    String? idempotencyKey,
  }) =>
      _request(
        'DELETE',
        '/app/projects/${_pathSegment(projectId)}',
        null,
        idempotencyKey ??
            'mobile-project-delete-$projectId-${DateTime.now().millisecondsSinceEpoch}',
      );

  Future<Map<String, dynamic>> registerMobile({
    required String deviceId,
    String? pushToken,
    String pushTokenType = 'fid',
    String? publicKey,
  }) =>
      _request(
        'POST',
        '/app/devices/register',
        <String, dynamic>{
          'device_id': deviceId,
          'device_type': 'mobile',
          'name': 'Omni Mobile',
          'platform': 'flutter',
          'push_token': pushToken,
          if (pushToken != null) 'push_token_type': pushTokenType,
          'public_key': publicKey,
          'capabilities': <String>[
            'chat',
            'chat-streaming',
            'approval',
            'notification',
          ],
        },
        'mobile-register-$deviceId',
      );

  Future<Map<String, dynamic>> createConversation(String title) => _request(
        'POST',
        '/app/conversations',
        <String, dynamic>{'title': title},
        'mobile-conversation-${title.hashCode}-${DateTime.now().millisecondsSinceEpoch}',
      );

  Future<Map<String, dynamic>> listMessages(String conversationId) =>
      _request(
        'GET',
        '/app/conversations/${_pathSegment(conversationId)}/messages',
      );

  Future<Map<String, dynamic>> askConversation(
    String conversationId,
    String content, {
    String modelProfile = 'fast',
    String? sourceDeviceId,
    String? idempotencyKey,
  }) =>
      _request(
        'POST',
        '/app/conversations/${_pathSegment(conversationId)}/ask',
        <String, dynamic>{
          'content': content,
          'model_profile': modelProfile,
          'stream': false,
          if (sourceDeviceId != null && sourceDeviceId.isNotEmpty)
            'source_device_id': sourceDeviceId,
        },
        idempotencyKey ??
            'mobile-ask-$conversationId-${content.hashCode}-${DateTime.now().millisecondsSinceEpoch}',
      );

  Stream<ChatStreamEvent> streamChat({
    String? conversationId,
    required String content,
    String modelProfile = 'fast',
    String? sourceDeviceId,
    String? idempotencyKey,
    int lastEventId = 0,
  }) async* {
    const path = '/api/chat/stream';
    final body = jsonEncode(<String, dynamic>{
      if (conversationId != null && conversationId.isNotEmpty)
        'conversation_id': conversationId,
      'content': content,
      'model_profile': modelProfile,
      if (sourceDeviceId != null && sourceDeviceId.isNotEmpty)
        'source_device_id': sourceDeviceId,
    });
    final key = idempotencyKey ??
        'mobile-stream-${conversationId ?? 'new'}-${DateTime.now().microsecondsSinceEpoch}';
    final request = http.Request(
      'POST',
      Uri.parse('${baseUrl.replaceAll(RegExp(r'/$'), '')}$path'),
    );
    request.body = body;
    request.headers.addAll(await _headers('POST', path, body, key));
    request.headers['accept'] = 'text/event-stream';
    if (lastEventId > 0) {
      request.headers['last-event-id'] = '$lastEventId';
    }

    final response = await _httpClient.send(request);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final responseBody = await response.stream.bytesToString();
      throw Exception(
        _safeStreamError(response.statusCode, responseBody),
      );
    }

    var currentId = 0;
    var currentEvent = '';
    final dataLines = <String>[];
    await for (final line in response.stream
        .transform(utf8.decoder)
        .transform(const LineSplitter())) {
      if (line.isEmpty) {
        final event = _buildStreamEvent(
          currentId,
          currentEvent,
          dataLines,
        );
        currentId = 0;
        currentEvent = '';
        dataLines.clear();
        if (event != null && event.id > lastEventId) {
          yield event;
          if (event.event == 'chat.failed') {
            throw Exception(
              'Omni stream failed (${event.data['code'] ?? 'chat_stream_failed'})',
            );
          }
        }
        continue;
      }
      if (line.startsWith(':')) continue;
      final separator = line.indexOf(':');
      final field = separator < 0 ? line : line.substring(0, separator);
      final value = separator < 0
          ? ''
          : line.substring(separator + 1).replaceFirst(RegExp(r'^ '), '');
      if (field == 'id') {
        currentId = int.tryParse(value) ?? 0;
      } else if (field == 'event') {
        currentEvent = value;
      } else if (field == 'data') {
        dataLines.add(value);
      }
    }
  }

  Future<Map<String, dynamic>> sendMessage(
    String conversationId,
    String content, {
    bool requiresDesktopRuntime = false,
    String risk = 'medium',
    String? idempotencyKey,
  }) =>
      _request(
        'POST',
        '/app/conversations/${_pathSegment(conversationId)}/messages',
        <String, dynamic>{
          'content': content,
          'requires_desktop_runtime': requiresDesktopRuntime,
          'risk': risk,
        },
        idempotencyKey ?? 'mobile-message-$conversationId-${content.hashCode}',
      );

  /// [approvalSignature] carries the biometric-gated approval key headers
  /// (see ApprovalKeyStore.signDecision).
  Future<Map<String, dynamic>> decideApproval(
    String approvalId,
    String decision, {
    String? reason,
    String? sourceDeviceId,
    String? idempotencyKey,
    Map<String, String> approvalSignature = const <String, String>{},
  }) =>
      _request(
        'POST',
        '/app/approvals/${_pathSegment(approvalId)}/decide',
        <String, dynamic>{
          'decision': decision,
          'reason': reason,
          if (sourceDeviceId != null && sourceDeviceId.isNotEmpty)
            'source_device_id': sourceDeviceId,
        },
        idempotencyKey ?? 'mobile-approval-$approvalId-$decision',
        approvalSignature,
      );

  /// Registers the biometric-gated approval key (write-once on the gateway).
  Future<Map<String, dynamic>> registerApprovalKey(
    String deviceId,
    String publicKey,
  ) => _request(
    'POST',
    '/app/devices/${_pathSegment(deviceId)}/approval-key',
    <String, dynamic>{'public_key': publicKey},
  );

  /// [pushToken] is a Firebase Installation ID by default (firebase_messaging
  /// 17 register()/onRegistered); pass tokenType 'fcm_token' for legacy tokens.
  Future<Map<String, dynamic>> registerPushToken(
    String deviceId,
    String pushToken, {
    String platform = 'flutter',
    String tokenType = 'fid',
  }) =>
      _request(
        'POST',
        '/app/devices/${_pathSegment(deviceId)}/push-token',
        <String, dynamic>{
          'push_token': pushToken,
          'token_type': tokenType,
          'platform': platform,
        },
      );

  /// Device enrollment (owner token): start, complete with this device's
  /// public key, then prove possession of the private key via a challenge.
  Future<Map<String, dynamic>> startDeviceEnrollment(String pairingCode) =>
      _request('POST', '/app/devices/enrollment/start', <String, dynamic>{
        'device_type': 'mobile',
        'pairing_code': pairingCode,
      });

  Future<Map<String, dynamic>> completeDeviceEnrollment(
    String enrollmentId,
    String pairingCode,
    String deviceId,
    String publicKey,
  ) => _request(
    'POST',
    '/app/devices/enrollment/${_pathSegment(enrollmentId)}/complete',
    <String, dynamic>{
      'pairing_code': pairingCode,
      'device_id': deviceId,
      'public_key': publicKey,
    },
  );

  Future<Map<String, dynamic>> issueDeviceChallenge(
    String enrollmentId,
    String deviceId,
  ) => _request(
    'POST',
    '/app/devices/enrollment/${_pathSegment(enrollmentId)}/challenge',
    <String, dynamic>{'device_id': deviceId},
  );

  Future<Map<String, dynamic>> verifyDeviceChallenge(
    String enrollmentId,
    String challengeId,
    String deviceId,
    String signature,
  ) => _request(
    'POST',
    '/app/devices/enrollment/${_pathSegment(enrollmentId)}/verify',
    <String, dynamic>{
      'challenge_id': challengeId,
      'device_id': deviceId,
      'signature': signature,
    },
  );

  Future<Map<String, dynamic>> notifications() =>
      _request('GET', '/app/notifications?audience=mobile');

  void close() => _httpClient.close();
}

ChatStreamEvent? _buildStreamEvent(
  int id,
  String event,
  List<String> dataLines,
) {
  if (id < 1 || event.isEmpty) return null;
  Map<String, dynamic> data = <String, dynamic>{};
  try {
    final decoded = jsonDecode(dataLines.join('\n'));
    if (decoded is Map<String, dynamic>) data = decoded;
  } catch (_) {
    data = <String, dynamic>{'code': 'invalid_stream_event'};
  }
  return ChatStreamEvent(id: id, event: event, data: data);
}

String _normalizeBaseUrl(String value) {
  final uri = Uri.parse(value.trim());
  if (!uri.hasScheme || !uri.hasAuthority) {
    throw ArgumentError('Omni API baseUrl must be an absolute URL');
  }
  final loopback = _isLoopbackHost(uri.host);
  if (uri.scheme != 'https' && !(uri.scheme == 'http' && loopback)) {
    throw ArgumentError(
      'Omni API baseUrl must use https unless it targets loopback',
    );
  }
  if (uri.hasQuery || uri.hasFragment || uri.userInfo.isNotEmpty) {
    throw ArgumentError(
      'Omni API baseUrl must not include credentials, query, or fragment',
    );
  }
  return value.replaceAll(RegExp(r'/$'), '');
}

bool _isLoopbackHost(String host) {
  final normalized = host.toLowerCase();
  return normalized == 'localhost' ||
      normalized == '127.0.0.1' ||
      normalized == '::1';
}

String _pathSegment(String value) => Uri.encodeComponent(value);

String _safeApiError(http.Response response) {
  return _safeStreamError(response.statusCode, response.body);
}

String _safeStreamError(int statusCode, String body) {
  var code = 'request_failed';
  try {
    final decoded = jsonDecode(body);
    if (decoded is Map<String, dynamic>) {
      final candidate = decoded['code'] ?? decoded['error'] ?? decoded['detail'];
      if (candidate is String &&
          RegExp(r'^[A-Za-z0-9_.:-]{1,80}$').hasMatch(candidate)) {
        code = candidate;
      }
    }
  } catch (_) {
    // Keep mobile UI/log errors body-free even when the gateway returns text.
  }
  return 'Omni API request failed ($statusCode, $code)';
}
