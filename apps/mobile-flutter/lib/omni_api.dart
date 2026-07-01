import 'dart:convert';
import 'package:http/http.dart' as http;
import 'device_identity.dart';

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
  ]) async {
    final uri = Uri.parse('${baseUrl.replaceAll(RegExp(r'/$'), '')}$path');
    late http.Response response;
    final requestBody =
        method == 'GET' ? '' : jsonEncode(body ?? <String, dynamic>{});
    final headers = await _headers(method, path, requestBody, idempotencyKey);
    if (method == 'GET') {
      response = await _httpClient.get(uri, headers: headers);
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

  Future<Map<String, dynamic>> bootstrap() => _request('GET', '/app/bootstrap');

  Future<Map<String, dynamic>> registerMobile({
    required String deviceId,
    String? pushToken,
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
            'public_key': publicKey,
            'capabilities': <String>['chat', 'approval', 'notification'],
          },
          'mobile-register-$deviceId');

  Future<Map<String, dynamic>> createConversation(String title) => _request(
        'POST',
        '/app/conversations',
        <String, dynamic>{'title': title},
        'mobile-conversation-${title.hashCode}-${DateTime.now().millisecondsSinceEpoch}',
      );

  Future<Map<String, dynamic>> listMessages(String conversationId) =>
      _request('GET', '/app/conversations/${_pathSegment(conversationId)}/messages');

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

  Future<Map<String, dynamic>> decideApproval(
    String approvalId,
    String decision, {
    String? reason,
    String? sourceDeviceId,
    String? idempotencyKey,
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
      );

  Future<Map<String, dynamic>> registerPushToken(
    String deviceId,
    String pushToken, {
    String platform = 'flutter',
  }) =>
      _request('POST', '/app/devices/${_pathSegment(deviceId)}/push-token', <String, dynamic>{
        'push_token': pushToken,
        'platform': platform,
      });

  Future<Map<String, dynamic>> notifications() =>
      _request('GET', '/app/notifications?audience=mobile');

  void close() => _httpClient.close();
}

String _normalizeBaseUrl(String value) {
  final uri = Uri.parse(value.trim());
  if (!uri.hasScheme || !uri.hasAuthority) {
    throw ArgumentError('Omni API baseUrl must be an absolute URL');
  }
  final loopback = _isLoopbackHost(uri.host);
  if (uri.scheme != 'https' && !(uri.scheme == 'http' && loopback)) {
    throw ArgumentError('Omni API baseUrl must use https unless it targets loopback');
  }
  if (uri.hasQuery || uri.hasFragment || uri.userInfo.isNotEmpty) {
    throw ArgumentError('Omni API baseUrl must not include credentials, query, or fragment');
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
  var code = 'request_failed';
  try {
    final decoded = jsonDecode(response.body);
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
  return 'Omni API request failed (${response.statusCode}, $code)';
}
