import 'dart:convert';
import 'package:http/http.dart' as http;
import 'device_identity.dart';

class OmniApiClient {
  OmniApiClient({
    required this.baseUrl,
    required this.token,
    required this.actor,
    http.Client? httpClient,
    DeviceIdentityStore? deviceIdentityStore,
  }) : _httpClient = httpClient ?? http.Client(),
       _deviceIdentityStore = deviceIdentityStore;

  final String baseUrl;
  final String token;
  final String actor;
  final http.Client _httpClient;
  final DeviceIdentityStore? _deviceIdentityStore;

  Future<Map<String, String>> _headers(String method, String path, String body, [String? idempotencyKey]) async {
    final signedHeaders = _deviceIdentityStore == null ? <String, String>{} : await _deviceIdentityStore.signRequest(method: method, path: path, body: body);
    return <String, String>{
    'content-type': 'application/json',
    'authorization': 'Bearer $token',
    'x-omnidesk-actor': actor,
    if (idempotencyKey != null && idempotencyKey.isNotEmpty) 'idempotency-key': idempotencyKey,
    ...signedHeaders,
  };
  }

  Future<Map<String, dynamic>> _request(String method, String path, [Map<String, dynamic>? body, String? idempotencyKey]) async {
    final uri = Uri.parse('${baseUrl.replaceAll(RegExp(r'/$'), '')}$path');
    late http.Response response;
    final requestBody = method == 'GET' ? '' : jsonEncode(body ?? <String, dynamic>{});
    final headers = await _headers(method, path, requestBody, idempotencyKey);
    if (method == 'GET') {
      response = await _httpClient.get(uri, headers: headers);
    } else {
      response = await _httpClient.post(uri, headers: headers, body: requestBody);
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('${response.statusCode}: ${response.body}');
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> bootstrap() => _request('GET', '/app/bootstrap');

  Future<Map<String, dynamic>> registerMobile({required String deviceId, String? pushToken, String? publicKey}) => _request('POST', '/app/devices/register', <String, dynamic>{
    'device_id': deviceId,
    'device_type': 'mobile',
    'name': 'Omni Mobile',
    'platform': 'flutter',
    'push_token': pushToken,
    'public_key': publicKey,
    'capabilities': <String>['chat', 'approval', 'notification'],
  }, 'mobile-register-$deviceId');

  Future<Map<String, dynamic>> createConversation(String title) => _request('POST', '/app/conversations', <String, dynamic>{'title': title}, 'mobile-conversation-${title.hashCode}-${DateTime.now().millisecondsSinceEpoch}');

  Future<Map<String, dynamic>> sendMessage(String conversationId, String content, {bool requiresDesktopRuntime = false, String risk = 'medium', String? idempotencyKey}) => _request('POST', '/app/conversations/$conversationId/messages', <String, dynamic>{
    'content': content,
    'requires_desktop_runtime': requiresDesktopRuntime,
    'risk': risk,
  }, idempotencyKey ?? 'mobile-message-$conversationId-${content.hashCode}');

  Future<Map<String, dynamic>> decideApproval(String approvalId, String decision, {String? reason, String? sourceDeviceId, String? idempotencyKey}) => _request('POST', '/app/approvals/$approvalId/decide', <String, dynamic>{
    'decision': decision,
    'reason': reason,
    if (sourceDeviceId != null && sourceDeviceId.isNotEmpty) 'source_device_id': sourceDeviceId,
  }, idempotencyKey ?? 'mobile-approval-$approvalId-$decision');

  Future<Map<String, dynamic>> registerPushToken(String deviceId, String pushToken, {String platform = 'flutter'}) => _request('POST', '/app/devices/$deviceId/push-token', <String, dynamic>{'push_token': pushToken, 'platform': platform});

  Future<Map<String, dynamic>> notifications() => _request('GET', '/app/notifications?audience=mobile');

  void close() => _httpClient.close();
}
