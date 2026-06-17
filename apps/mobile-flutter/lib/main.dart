import 'dart:math' as math;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

import 'device_identity.dart';
import 'mobile_config.dart';
import 'omni_api.dart';

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
  final gatewayController =
      TextEditingController(text: OmniMobileConfig.defaultGatewayUrl);
  final tokenController = TextEditingController();
  final actorController = TextEditingController(text: 'mobile-operator');
  final taskController = TextEditingController(text: '请检查今天的任务状态');
  final commandController = TextEditingController();
  final reasonController = TextEditingController(
      text: 'Approved from Omni Mobile with biometric/PIN confirmation');

  final List<_SessionItem> sessions = const <_SessionItem>[
    _SessionItem(
      title: '帮我分析销售数据',
      subtitle: '拉取数据并生成图表',
      time: '10:30',
      icon: Icons.analytics_outlined,
    ),
    _SessionItem(
      title: '生成周报并发送运营邮箱',
      subtitle: '待审计',
      time: '09:15',
      icon: Icons.description_outlined,
    ),
    _SessionItem(
      title: '查询今天的日程安排',
      subtitle: '已同步',
      time: '昨天',
      icon: Icons.calendar_month_outlined,
    ),
  ];

  final List<String> commandHistory = <String>['帮我分析上个月的销售数据，生成图表'];

  Map<String, dynamic>? snapshot;
  int selectedIndex = 0;
  bool detailOpen = false;
  String error = '';
  String securityState = 'secure storage ready';
  String pushState = 'push not registered';
  String statusMessage = '1.11.4 mobile cockpit ready';
  OmniDeviceIdentity? deviceIdentity;

  DeviceIdentityStore get identityStore => DeviceIdentityStore(_storage);
  OmniApiClient get client => OmniApiClient(
      baseUrl: gatewayController.text,
      token: tokenController.text,
      actor: actorController.text,
      deviceIdentityStore: identityStore);

  @override
  void initState() {
    super.initState();
    _restoreSession();
  }

  @override
  void dispose() {
    gatewayController.dispose();
    tokenController.dispose();
    actorController.dispose();
    taskController.dispose();
    commandController.dispose();
    reasonController.dispose();
    super.dispose();
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
        options:
            const AuthenticationOptions(biometricOnly: false, stickyAuth: true),
      );
    } catch (_) {
      return false;
    }
  }

  Future<_PushTokenResult> _resolvePushToken() async {
    if (Firebase.apps.isEmpty) {
      return const _PushTokenResult(
          null, 'push unavailable: Firebase config missing');
    }
    try {
      final messaging = FirebaseMessaging.instance;
      await messaging.requestPermission(alert: true, badge: true, sound: true);
      final token = await messaging.getToken();
      if (token == null || token.isEmpty) {
        return const _PushTokenResult(
            null, 'push unavailable: provider returned no token');
      }
      return _PushTokenResult(token, 'FCM/APNS token registered');
    } catch (e) {
      return _PushTokenResult(null, _pushUnavailableMessage(e));
    }
  }

  String _pushUnavailableMessage(Object error) {
    final message = error.toString();
    if (message.contains('aps-environment')) {
      return 'push unavailable: APNS entitlement missing';
    }
    if (message.contains('GoogleService-Info') ||
        message.contains('Firebase')) {
      return 'push unavailable: Firebase config missing';
    }
    return 'push unavailable: ${message.split('\n').first}';
  }

  Future<void> connect() async {
    setState(() {
      error = '';
      statusMessage = '正在连接 Omni Gateway';
    });
    final gatewayError =
        OmniMobileConfig.validateGatewayUrl(gatewayController.text);
    if (gatewayError != null) {
      setState(() {
        error = gatewayError;
        statusMessage = 'Gateway 配置未通过 preflight';
      });
      return;
    }
    try {
      await _saveSession();
      final pushToken = await _resolvePushToken();
      final identity = await identityStore.loadOrCreate();
      deviceIdentity = identity;
      await client.registerMobile(
          deviceId: identity.deviceId,
          pushToken: pushToken.token,
          publicKey: identity.publicKey);
      if (pushToken.token != null) {
        await client.registerPushToken(identity.deviceId, pushToken.token!);
      }
      snapshot = await client.bootstrap();
      setState(() {
        securityState =
            'session saved in flutter_secure_storage; biometric/PIN required for approval';
        pushState = pushToken.status;
        statusMessage = 'Gateway 已连接，审批与通知链路已刷新';
      });
    } catch (e) {
      setState(() {
        error = e.toString();
        statusMessage = 'Gateway 连接失败';
      });
    }
  }

  Future<void> sendTask() async {
    try {
      final conv = await client.createConversation('Mobile Chat');
      await client.sendMessage(
          conv['conversation']['conversation_id'] as String,
          taskController.text,
          requiresDesktopRuntime: true,
          risk: 'high');
      snapshot = await client.bootstrap();
      setState(() {
        statusMessage = '已发送任务并请求桌面执行';
      });
    } catch (e) {
      setState(() {
        error = e.toString();
        statusMessage = '任务发送失败';
      });
    }
  }

  Future<void> decide(String approvalId, String decision) async {
    try {
      final confirmed = await _confirmSensitiveAction();
      if (!confirmed) {
        setState(() => error = '审批被本机生物识别/PIN取消。');
        return;
      }
      await client.decideApproval(approvalId, decision,
          reason: reasonController.text,
          sourceDeviceId: deviceIdentity?.deviceId);
      snapshot = await client.bootstrap();
      setState(() {
        statusMessage = decision == 'approved' ? '审批已通过' : '审批已拒绝';
      });
    } catch (e) {
      setState(() => error = e.toString());
    }
  }

  void openAnalysis() {
    setState(() {
      detailOpen = true;
      selectedIndex = 1;
      statusMessage = '销售分析会话已打开';
    });
  }

  void submitCommand() {
    final command = commandController.text.trim();
    if (command.isEmpty) return;
    setState(() {
      commandHistory.add(command);
      commandController.clear();
      statusMessage = '已生成新的分析卡片';
      detailOpen = true;
      selectedIndex = 1;
    });
  }

  Future<void> copyReport() async {
    try {
      await Clipboard.setData(
          const ClipboardData(text: '销售数据分析报告：上月销售额环比增长 12.5%，电子产品和家居用品增长明显。'));
      setState(() => statusMessage = '报告摘要已复制');
    } catch (_) {
      setState(() => statusMessage = '报告摘要已准备复制');
    }
  }

  void exportReport() {
    setState(() => statusMessage = '已导出销售分析报告草稿');
  }

  void sendReport() {
    setState(() => statusMessage = '已发送到审批/审计链路');
  }

  @override
  Widget build(BuildContext context) {
    final approvals =
        (snapshot?['pending_approvals'] as List<dynamic>? ?? <dynamic>[]);
    final notifications =
        (snapshot?['notifications'] as List<dynamic>? ?? <dynamic>[]);
    return MaterialApp(
      title: 'Omni Mobile',
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF2563EB),
          brightness: Brightness.light,
        ),
        scaffoldBackgroundColor: const Color(0xFFF7F8FB),
        cardTheme: const CardThemeData(
          elevation: 0,
          color: Colors.white,
          surfaceTintColor: Colors.white,
          margin: EdgeInsets.zero,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(8)),
          ),
        ),
      ),
      home: Scaffold(
        body: SafeArea(
          child: detailOpen
              ? _AnalysisDetailView(
                  commandController: commandController,
                  commandHistory: commandHistory,
                  statusMessage: statusMessage,
                  onBack: () => setState(() => detailOpen = false),
                  onCopy: copyReport,
                  onExport: exportReport,
                  onSend: sendReport,
                  onSubmit: submitCommand,
                )
              : IndexedStack(
                  index: selectedIndex,
                  children: <Widget>[
                    _HomeView(
                      sessions: sessions,
                      approvals: approvals,
                      notifications: notifications,
                      statusMessage: statusMessage,
                      onOpenAnalysis: openAnalysis,
                    ),
                    _ConversationView(
                      sessions: sessions,
                      statusMessage: statusMessage,
                      onOpenAnalysis: openAnalysis,
                    ),
                    _ToolsView(onOpenAnalysis: openAnalysis),
                    _SettingsView(
                      gatewayController: gatewayController,
                      tokenController: tokenController,
                      actorController: actorController,
                      taskController: taskController,
                      reasonController: reasonController,
                      approvals: approvals,
                      notifications: notifications,
                      error: error,
                      securityState: securityState,
                      pushState: pushState,
                      deviceId: deviceIdentity?.deviceId,
                      onConnect: connect,
                      onSendTask: sendTask,
                      onDecide: decide,
                    ),
                  ],
                ),
        ),
        bottomNavigationBar: detailOpen
            ? null
            : NavigationBar(
                selectedIndex: selectedIndex,
                onDestinationSelected: (index) =>
                    setState(() => selectedIndex = index),
                destinations: const <NavigationDestination>[
                  NavigationDestination(
                    icon: Icon(Icons.home_outlined),
                    selectedIcon: Icon(Icons.home),
                    label: '首页',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.chat_bubble_outline),
                    selectedIcon: Icon(Icons.chat_bubble),
                    label: '会话',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.hub_outlined),
                    selectedIcon: Icon(Icons.hub),
                    label: '工具',
                  ),
                  NavigationDestination(
                    icon: Icon(Icons.settings_outlined),
                    selectedIcon: Icon(Icons.settings),
                    label: '设置',
                  ),
                ],
              ),
        floatingActionButton: !detailOpen && selectedIndex == 0
            ? FloatingActionButton(
                onPressed: openAnalysis,
                child: const Icon(Icons.add),
              )
            : null,
      ),
    );
  }
}

class _HomeView extends StatelessWidget {
  const _HomeView({
    required this.sessions,
    required this.approvals,
    required this.notifications,
    required this.statusMessage,
    required this.onOpenAnalysis,
  });

  final List<_SessionItem> sessions;
  final List<dynamic> approvals;
  final List<dynamic> notifications;
  final String statusMessage;
  final VoidCallback onOpenAnalysis;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
      children: <Widget>[
        Row(
          children: <Widget>[
            Expanded(
              child: Text(
                'OmniDesk AI',
                style: Theme.of(context)
                    .textTheme
                    .titleLarge
                    ?.copyWith(fontWeight: FontWeight.w800),
              ),
            ),
            IconButton.filledTonal(
              tooltip: '审计状态',
              onPressed: () {},
              icon: const Icon(Icons.verified_user_outlined),
            ),
          ],
        ),
        const SizedBox(height: 18),
        Text(
          'Hello, OmniDesk',
          style: Theme.of(context)
              .textTheme
              .headlineSmall
              ?.copyWith(fontWeight: FontWeight.w800),
        ),
        const SizedBox(height: 14),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  '今日摘要',
                  style: Theme.of(context)
                      .textTheme
                      .labelLarge
                      ?.copyWith(color: Colors.black54),
                ),
                const SizedBox(height: 18),
                const Row(
                  children: <Widget>[
                    Expanded(
                      child: _MetricTile(
                          value: '12', label: '任务完成', color: Colors.green),
                    ),
                    _VerticalDivider(),
                    Expanded(
                      child: _MetricTile(
                          value: '3', label: '待审批', color: Colors.redAccent),
                    ),
                    _VerticalDivider(),
                    Expanded(
                      child: _MetricTile(
                          value: '98%', label: '成功率', color: Colors.green),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 22),
        Text(
          '最近会话',
          style: Theme.of(context)
              .textTheme
              .titleMedium
              ?.copyWith(fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 10),
        Card(
          child: Column(
            children: <Widget>[
              for (var index = 0; index < sessions.length; index++)
                _SessionTile(
                  item: sessions[index],
                  showDivider: index != sessions.length - 1,
                  onTap: index == 0 ? onOpenAnalysis : null,
                ),
            ],
          ),
        ),
        const SizedBox(height: 20),
        _SignalStrip(
          approvals: approvals.length,
          notifications: notifications.length,
          message: statusMessage,
        ),
      ],
    );
  }
}

class _ConversationView extends StatelessWidget {
  const _ConversationView({
    required this.sessions,
    required this.statusMessage,
    required this.onOpenAnalysis,
  });

  final List<_SessionItem> sessions;
  final String statusMessage;
  final VoidCallback onOpenAnalysis;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
      children: <Widget>[
        Text(
          '会话',
          style: Theme.of(context)
              .textTheme
              .headlineSmall
              ?.copyWith(fontWeight: FontWeight.w800),
        ),
        const SizedBox(height: 14),
        Card(
          child: Column(
            children: <Widget>[
              for (var index = 0; index < sessions.length; index++)
                _SessionTile(
                  item: sessions[index],
                  showDivider: index != sessions.length - 1,
                  onTap: index == 0 ? onOpenAnalysis : null,
                ),
            ],
          ),
        ),
        const SizedBox(height: 18),
        _InfoCard(
          icon: Icons.timeline_outlined,
          title: '会话链路',
          body: statusMessage,
        ),
      ],
    );
  }
}

class _ToolsView extends StatelessWidget {
  const _ToolsView({required this.onOpenAnalysis});

  final VoidCallback onOpenAnalysis;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
      children: <Widget>[
        Text(
          '工具',
          style: Theme.of(context)
              .textTheme
              .headlineSmall
              ?.copyWith(fontWeight: FontWeight.w800),
        ),
        const SizedBox(height: 14),
        _ToolButton(
          icon: Icons.bar_chart_outlined,
          title: '销售数据分析',
          body: '生成柱状图、饼图和摘要',
          onTap: onOpenAnalysis,
        ),
        const SizedBox(height: 10),
        _ToolButton(
          icon: Icons.fact_check_outlined,
          title: '审批审计',
          body: '查看待审批、审批原因和设备签名状态',
          onTap: () {},
        ),
        const SizedBox(height: 10),
        _ToolButton(
          icon: Icons.notifications_active_outlined,
          title: '通知链路',
          body: '检查移动端通知、桌面执行和会话回执',
          onTap: () {},
        ),
      ],
    );
  }
}

class _SettingsView extends StatelessWidget {
  const _SettingsView({
    required this.gatewayController,
    required this.tokenController,
    required this.actorController,
    required this.taskController,
    required this.reasonController,
    required this.approvals,
    required this.notifications,
    required this.error,
    required this.securityState,
    required this.pushState,
    required this.deviceId,
    required this.onConnect,
    required this.onSendTask,
    required this.onDecide,
  });

  final TextEditingController gatewayController;
  final TextEditingController tokenController;
  final TextEditingController actorController;
  final TextEditingController taskController;
  final TextEditingController reasonController;
  final List<dynamic> approvals;
  final List<dynamic> notifications;
  final String error;
  final String securityState;
  final String pushState;
  final String? deviceId;
  final Future<void> Function() onConnect;
  final Future<void> Function() onSendTask;
  final Future<void> Function(String approvalId, String decision) onDecide;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
      children: <Widget>[
        Text(
          '设置',
          style: Theme.of(context)
              .textTheme
              .headlineSmall
              ?.copyWith(fontWeight: FontWeight.w800),
        ),
        const SizedBox(height: 14),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                Text('Security: $securityState'),
                const SizedBox(height: 6),
                Text('Push: $pushState'),
                const SizedBox(height: 6),
                Text('Device: ${deviceId ?? 'not enrolled'}'),
                const SizedBox(height: 14),
                TextField(
                  controller: gatewayController,
                  decoration: const InputDecoration(labelText: 'Gateway URL'),
                ),
                TextField(
                  controller: tokenController,
                  decoration:
                      const InputDecoration(labelText: 'Owner/Operator Token'),
                  obscureText: true,
                ),
                TextField(
                  controller: actorController,
                  decoration: const InputDecoration(labelText: 'Actor'),
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  onPressed: onConnect,
                  icon: const Icon(Icons.link),
                  label: const Text('连接 Omni Gateway'),
                ),
                if (error.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text(
                      error,
                      style: const TextStyle(color: Colors.red),
                    ),
                  ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 16),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                TextField(
                  controller: taskController,
                  decoration: const InputDecoration(labelText: '发送任务'),
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  onPressed: onSendTask,
                  icon: const Icon(Icons.send_outlined),
                  label: const Text('发送并请求桌面执行'),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 16),
        Text(
          '待审批：${approvals.length}',
          style: Theme.of(context).textTheme.titleLarge,
        ),
        const SizedBox(height: 8),
        TextField(
          controller: reasonController,
          decoration: const InputDecoration(labelText: '审批原因 / Audit Reason'),
        ),
        const SizedBox(height: 10),
        for (final approval in approvals)
          Card(
            child: ListTile(
              title: Text(approval['action']?.toString() ?? 'Approval'),
              subtitle: Text(
                  'Risk: ${approval['risk']}\nReason: ${approval['reason']}\nExpires: ${approval['expires_at'] ?? 'n/a'}'),
              isThreeLine: true,
              trailing: Wrap(spacing: 8, children: <Widget>[
                IconButton(
                    tooltip: '通过',
                    icon: const Icon(Icons.check),
                    onPressed: () => onDecide(
                        approval['approval_id'] as String, 'approved')),
                IconButton(
                    tooltip: '拒绝',
                    icon: const Icon(Icons.close),
                    onPressed: () => onDecide(
                        approval['approval_id'] as String, 'rejected')),
              ]),
            ),
          ),
        const SizedBox(height: 16),
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
    );
  }
}

class _AnalysisDetailView extends StatelessWidget {
  const _AnalysisDetailView({
    required this.commandController,
    required this.commandHistory,
    required this.statusMessage,
    required this.onBack,
    required this.onCopy,
    required this.onExport,
    required this.onSend,
    required this.onSubmit,
  });

  final TextEditingController commandController;
  final List<String> commandHistory;
  final String statusMessage;
  final VoidCallback onBack;
  final Future<void> Function() onCopy;
  final VoidCallback onExport;
  final VoidCallback onSend;
  final VoidCallback onSubmit;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 8, 4),
          child: Row(
            children: <Widget>[
              IconButton(
                tooltip: '返回',
                icon: const Icon(Icons.arrow_back),
                onPressed: onBack,
              ),
              Expanded(
                child: Text(
                  '帮我分析销售数据',
                  style: Theme.of(context)
                      .textTheme
                      .titleMedium
                      ?.copyWith(fontWeight: FontWeight.w800),
                ),
              ),
              IconButton(
                tooltip: '会话工具',
                icon: const Icon(Icons.dashboard_customize_outlined),
                onPressed: () {},
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(20, 14, 20, 18),
            children: <Widget>[
              for (final command in commandHistory)
                Align(
                  alignment: Alignment.centerRight,
                  child: Container(
                    constraints: const BoxConstraints(maxWidth: 270),
                    margin: const EdgeInsets.only(bottom: 12),
                    padding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 12),
                    decoration: BoxDecoration(
                      color: const Color(0xFF2563EB),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      command,
                      style: const TextStyle(
                        color: Colors.white,
                        height: 1.35,
                      ),
                    ),
                  ),
                ),
              _AnalysisReportCard(
                statusMessage: statusMessage,
                onCopy: onCopy,
                onExport: onExport,
                onSend: onSend,
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 18),
          child: Row(
            children: <Widget>[
              Expanded(
                child: TextField(
                  controller: commandController,
                  minLines: 1,
                  maxLines: 3,
                  decoration: InputDecoration(
                    hintText: '输入你的指令...',
                    filled: true,
                    fillColor: Colors.white,
                    contentPadding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 12),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(28),
                      borderSide: BorderSide.none,
                    ),
                    suffixIcon: IconButton(
                      tooltip: '语音输入',
                      icon: const Icon(Icons.mic_none),
                      onPressed: () {},
                    ),
                  ),
                  onSubmitted: (_) => onSubmit(),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(
                tooltip: '发送',
                onPressed: onSubmit,
                icon: const Icon(Icons.send),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _AnalysisReportCard extends StatelessWidget {
  const _AnalysisReportCard({
    required this.statusMessage,
    required this.onCopy,
    required this.onExport,
    required this.onSend,
  });

  final String statusMessage;
  final Future<void> Function() onCopy;
  final VoidCallback onExport;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                const Icon(Icons.check_box, color: Color(0xFF16A34A)),
                const SizedBox(width: 8),
                Text(
                  '已完成分析',
                  style: Theme.of(context)
                      .textTheme
                      .labelLarge
                      ?.copyWith(fontWeight: FontWeight.w700),
                ),
              ],
            ),
            const SizedBox(height: 18),
            Text(
              '销售数据分析报告',
              style: Theme.of(context)
                  .textTheme
                  .titleMedium
                  ?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 12),
            const Row(
              children: <Widget>[
                Expanded(child: _BarPreview()),
                SizedBox(width: 14),
                SizedBox(width: 104, height: 104, child: _PiePreview()),
              ],
            ),
            const SizedBox(height: 18),
            Text(
              '总结',
              style: Theme.of(context)
                  .textTheme
                  .titleSmall
                  ?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 6),
            const Text(
              '上月销售额较上月增长 12.5%，其中电子产品和家居用品增长明显。',
              style: TextStyle(height: 1.45),
            ),
            const SizedBox(height: 12),
            Text(
              statusMessage,
              style: const TextStyle(color: Colors.black54),
            ),
            const SizedBox(height: 14),
            Row(
              children: <Widget>[
                _ReportActionButton(
                  icon: Icons.copy_outlined,
                  label: '复制',
                  onPressed: onCopy,
                ),
                const SizedBox(width: 8),
                _ReportActionButton(
                  icon: Icons.file_download_outlined,
                  label: '导出',
                  onPressed: onExport,
                ),
                const SizedBox(width: 8),
                _ReportActionButton(
                  icon: Icons.ios_share_outlined,
                  label: '发送',
                  onPressed: onSend,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _BarPreview extends StatelessWidget {
  const _BarPreview();

  static const values = <double>[0.42, 0.64, 0.55, 0.76, 0.68, 0.86];

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 110,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: <Widget>[
          for (var index = 0; index < values.length; index++)
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 4),
                child: FractionallySizedBox(
                  heightFactor: values[index],
                  alignment: Alignment.bottomCenter,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: Color.lerp(
                        const Color(0xFF60A5FA),
                        const Color(0xFF2563EB),
                        index / values.length,
                      ),
                      borderRadius: BorderRadius.circular(4),
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _PiePreview extends StatelessWidget {
  const _PiePreview();

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      painter: _PiePreviewPainter(),
      child: const SizedBox.expand(),
    );
  }
}

class _PiePreviewPainter extends CustomPainter {
  final List<_PieSlice> slices = const <_PieSlice>[
    _PieSlice(0.34, Color(0xFF2563EB)),
    _PieSlice(0.24, Color(0xFF60A5FA)),
    _PieSlice(0.22, Color(0xFF86EFAC)),
    _PieSlice(0.20, Color(0xFFE879F9)),
  ];

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    final paint = Paint()..style = PaintingStyle.fill;
    var start = -math.pi / 2;
    for (final slice in slices) {
      final sweep = slice.value * math.pi * 2;
      paint.color = slice.color;
      canvas.drawArc(rect, start, sweep, true, paint);
      start += sweep;
    }
    final border = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..color = Colors.white;
    canvas.drawCircle(rect.center, size.shortestSide / 2, border);
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

class _SessionTile extends StatelessWidget {
  const _SessionTile({
    required this.item,
    required this.showDivider,
    required this.onTap,
  });

  final _SessionItem item;
  final bool showDivider;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: <Widget>[
        ListTile(
          onTap: onTap,
          leading: CircleAvatar(
            radius: 16,
            backgroundColor: const Color(0xFFEFF6FF),
            child: Icon(item.icon, color: const Color(0xFF2563EB), size: 18),
          ),
          title: Text(
            item.title,
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
          subtitle: Text(item.subtitle),
          trailing: Text(
            item.time,
            style: const TextStyle(color: Colors.black54),
          ),
        ),
        if (showDivider) const Divider(height: 1, indent: 64, endIndent: 16),
      ],
    );
  }
}

class _MetricTile extends StatelessWidget {
  const _MetricTile({
    required this.value,
    required this.label,
    required this.color,
  });

  final String value;
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: <Widget>[
        Text(
          value,
          style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                color: color,
                fontWeight: FontWeight.w900,
              ),
        ),
        const SizedBox(height: 4),
        Text(
          label,
          style: Theme.of(context).textTheme.labelMedium,
          textAlign: TextAlign.center,
        ),
      ],
    );
  }
}

class _VerticalDivider extends StatelessWidget {
  const _VerticalDivider();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 1,
      height: 42,
      color: const Color(0xFFE5E7EB),
    );
  }
}

class _SignalStrip extends StatelessWidget {
  const _SignalStrip({
    required this.approvals,
    required this.notifications,
    required this.message,
  });

  final int approvals;
  final int notifications;
  final String message;

  @override
  Widget build(BuildContext context) {
    return _InfoCard(
      icon: Icons.shield_outlined,
      title: '审批 / 审计 / 通知',
      body: '待审批 $approvals，通知 $notifications。$message',
    );
  }
}

class _InfoCard extends StatelessWidget {
  const _InfoCard({
    required this.icon,
    required this.title,
    required this.body,
  });

  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: <Widget>[
            Icon(icon, color: const Color(0xFF2563EB)),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(
                    title,
                    style: const TextStyle(fontWeight: FontWeight.w800),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    body,
                    style: const TextStyle(color: Colors.black54, height: 1.35),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ToolButton extends StatelessWidget {
  const _ToolButton({
    required this.icon,
    required this.title,
    required this.body,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String body;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        onTap: onTap,
        leading: Icon(icon, color: const Color(0xFF2563EB)),
        title: Text(title, style: const TextStyle(fontWeight: FontWeight.w800)),
        subtitle: Text(body),
        trailing: const Icon(Icons.chevron_right),
      ),
    );
  }
}

class _ReportActionButton extends StatelessWidget {
  const _ReportActionButton({
    required this.icon,
    required this.label,
    required this.onPressed,
  });

  final IconData icon;
  final String label;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: OutlinedButton.icon(
        onPressed: onPressed,
        icon: Icon(icon, size: 18),
        label: FittedBox(child: Text(label)),
      ),
    );
  }
}

class _SessionItem {
  const _SessionItem({
    required this.title,
    required this.subtitle,
    required this.time,
    required this.icon,
  });

  final String title;
  final String subtitle;
  final String time;
  final IconData icon;
}

class _PieSlice {
  const _PieSlice(this.value, this.color);

  final double value;
  final Color color;
}

class _PushTokenResult {
  const _PushTokenResult(this.token, this.status);

  final String? token;
  final String status;
}
