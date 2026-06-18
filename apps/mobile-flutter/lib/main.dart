import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:local_auth/local_auth.dart';
import 'omni_api.dart';
import 'device_identity.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await Firebase.initializeApp();
  } catch (_) {
    // Enterprise-staging builds can run without platform Firebase config.
  }
  runApp(const OmniMobileApp());
}

class OmniMobileApp extends StatefulWidget {
  const OmniMobileApp({super.key});

  @override
  State<OmniMobileApp> createState() => _OmniMobileAppState();
}

class _OmniMobileAppState extends State<OmniMobileApp> {
  static const _storage = FlutterSecureStorage();
  final _auth = LocalAuthentication();
  final gatewayController = TextEditingController(
    text: 'http://127.0.0.1:18789',
  );
  final tokenController = TextEditingController();
  final actorController = TextEditingController(text: 'mobile-operator');
  final taskController = TextEditingController(text: '请检查今天的任务状态');
  final reasonController = TextEditingController(
    text: 'Approved from Omni Mobile with biometric/PIN confirmation',
  );
  Map<String, dynamic>? snapshot;
  String? chatConversationId;
  String chatProfile = 'fast';
  List<dynamic> chatMessages = <dynamic>[];
  String error = '';
  String securityState = 'secure storage ready';
  String pushState = 'push not registered';
  OmniDeviceIdentity? deviceIdentity;

  DeviceIdentityStore get identityStore => DeviceIdentityStore(_storage);
  OmniApiClient get client => OmniApiClient(
        baseUrl: gatewayController.text,
        token: tokenController.text,
        actor: actorController.text,
        deviceIdentityStore: identityStore,
      );

  @override
  void initState() {
    super.initState();
    _restoreSession();
  }

  Future<void> _restoreSession() async {
    gatewayController.text =
        await _storage.read(key: 'omni.gateway') ?? gatewayController.text;
    tokenController.text = await _storage.read(key: 'omni.token') ?? '';
    actorController.text =
        await _storage.read(key: 'omni.actor') ?? actorController.text;
    if (mounted) setState(() {});
  }

  Future<void> _saveSession() async {
    await _storage.write(key: 'omni.gateway', value: gatewayController.text);
    await _storage.write(key: 'omni.token', value: tokenController.text);
    await _storage.write(key: 'omni.actor', value: actorController.text);
  }

  Future<bool> _confirmSensitiveAction() async {
    try {
      final canCheck =
          await _auth.canCheckBiometrics || await _auth.isDeviceSupported();
      if (!canCheck) return false;
      return _auth.authenticate(
        localizedReason: 'Confirm Omni approval decision',
        options: const AuthenticationOptions(
          biometricOnly: false,
          stickyAuth: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }

  Future<String?> _resolvePushToken() async {
    try {
      final messaging = FirebaseMessaging.instance;
      await messaging.requestPermission(alert: true, badge: true, sound: true);
      return messaging.getToken();
    } catch (_) {
      return null;
    }
  }

  Future<void> connect() async {
    setState(() => error = '');
    try {
      await _saveSession();
      final token = await _resolvePushToken();
      final identity = await identityStore.loadOrCreate();
      deviceIdentity = identity;
      await client.registerMobile(
        deviceId: identity.deviceId,
        pushToken: token,
        publicKey: identity.publicKey,
      );
      if (token != null) {
        await client.registerPushToken(identity.deviceId, token);
      }
      snapshot = await client.bootstrap();
      setState(() {
        securityState =
            'session saved in flutter_secure_storage; biometric/PIN required for approval';
        pushState = token == null
            ? 'push provider unavailable in this build'
            : 'FCM/APNS token registered';
      });
    } catch (e) {
      setState(() => error = e.toString());
    }
  }

  Future<void> sendTask() async {
    try {
      final conv = await client.createConversation('Mobile Chat');
      await client.sendMessage(
        conv['conversation']['conversation_id'] as String,
        taskController.text,
        requiresDesktopRuntime: true,
        risk: 'high',
      );
      snapshot = await client.bootstrap();
      setState(() {});
    } catch (e) {
      setState(() => error = e.toString());
    }
  }

  Future<void> askAssistant() async {
    try {
      final identity = deviceIdentity ?? await identityStore.loadOrCreate();
      deviceIdentity = identity;
      var conversationId = chatConversationId;
      if (conversationId == null || conversationId.isEmpty) {
        final conv = await client.createConversation('Mobile Ask Mode');
        conversationId = conv['conversation']['conversation_id'] as String;
        chatConversationId = conversationId;
      }
      await client.askConversation(
        conversationId,
        taskController.text,
        modelProfile: chatProfile,
        sourceDeviceId: identity.deviceId,
      );
      final messages = await client.listMessages(conversationId);
      chatMessages = messages['messages'] as List<dynamic>? ?? <dynamic>[];
      snapshot = await client.bootstrap();
      setState(() {});
    } catch (e) {
      setState(() => error = e.toString());
    }
  }

  Future<void> decide(String approvalId, String decision) async {
    try {
      final confirmed = await _confirmSensitiveAction();
      if (!confirmed) {
        setState(() => error = '审批被本机生物识别/PIN取消。');
        return;
      }
      await client.decideApproval(
        approvalId,
        decision,
        reason: reasonController.text,
        sourceDeviceId: deviceIdentity?.deviceId,
      );
      snapshot = await client.bootstrap();
      setState(() {});
    } catch (e) {
      setState(() => error = e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final approvals =
        (snapshot?['pending_approvals'] as List<dynamic>? ?? <dynamic>[]);
    final notifications =
        (snapshot?['notifications'] as List<dynamic>? ?? <dynamic>[]);
    return MaterialApp(
      title: 'Omni Mobile',
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.indigo),
      home: Scaffold(
        appBar: AppBar(title: const Text('Omni Mobile Approval')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: <Widget>[
            Text('Security: $securityState'),
            Text('Push: $pushState'),
            Text('Device: ${deviceIdentity?.deviceId ?? 'not enrolled'}'),
            TextField(
              controller: gatewayController,
              decoration: const InputDecoration(labelText: 'Gateway URL'),
            ),
            TextField(
              controller: tokenController,
              decoration: const InputDecoration(
                labelText: 'Owner/Operator Token',
              ),
              obscureText: true,
            ),
            TextField(
              controller: actorController,
              decoration: const InputDecoration(labelText: 'Actor'),
            ),
            const SizedBox(height: 12),
            FilledButton(
              onPressed: connect,
              child: const Text('连接 Omni Gateway'),
            ),
            if (error.isNotEmpty)
              Text(error, style: const TextStyle(color: Colors.red)),
            const Divider(),
            DropdownButtonFormField<String>(
              initialValue: chatProfile,
              decoration: const InputDecoration(labelText: 'Model Profile'),
              items: const <DropdownMenuItem<String>>[
                DropdownMenuItem<String>(value: 'fast', child: Text('fast')),
                DropdownMenuItem<String>(
                  value: 'planner',
                  child: Text('planner'),
                ),
                DropdownMenuItem<String>(value: 'local', child: Text('local')),
              ],
              onChanged: (value) =>
                  setState(() => chatProfile = value ?? 'fast'),
            ),
            TextField(
              controller: taskController,
              decoration: const InputDecoration(labelText: '消息 / 任务内容'),
            ),
            Wrap(
              spacing: 8,
              children: <Widget>[
                FilledButton(
                  onPressed: askAssistant,
                  child: const Text('问一下 AI'),
                ),
                OutlinedButton(
                  onPressed: sendTask,
                  child: const Text('发送并请求桌面执行'),
                ),
              ],
            ),
            for (final message in chatMessages.take(8))
              ListTile(
                title: Text('${message['role'] ?? 'message'}'),
                subtitle: Text(
                  '${message['content'] ?? ''}\n${message['model_provider'] ?? ''} ${message['model_name'] ?? ''} ${message['trace_id'] ?? ''}',
                ),
                isThreeLine: true,
              ),
            const SizedBox(height: 12),
            Text(
              '待审批：${approvals.length}',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            TextField(
              controller: reasonController,
              decoration: const InputDecoration(
                labelText: '审批原因 / Audit Reason',
              ),
            ),
            for (final approval in approvals)
              Card(
                child: ListTile(
                  title: Text(approval['action']?.toString() ?? 'Approval'),
                  subtitle: Text(
                    'Risk: ${approval['risk']}\nReason: ${approval['reason']}\nExpires: ${approval['expires_at'] ?? 'n/a'}',
                  ),
                  isThreeLine: true,
                  trailing: Wrap(
                    spacing: 8,
                    children: <Widget>[
                      IconButton(
                        icon: const Icon(Icons.check),
                        onPressed: () => decide(
                          approval['approval_id'] as String,
                          'approved',
                        ),
                      ),
                      IconButton(
                        icon: const Icon(Icons.close),
                        onPressed: () => decide(
                          approval['approval_id'] as String,
                          'rejected',
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            const Divider(),
            Text(
              '通知：${notifications.length}',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            for (final item in notifications.take(8))
              ListTile(
                title: Text(item['title']?.toString() ?? ''),
                subtitle: Text(item['body']?.toString() ?? ''),
              ),
          ],
        ),
      ),
    );
  }
}
