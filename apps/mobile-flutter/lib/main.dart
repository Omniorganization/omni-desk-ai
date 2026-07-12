import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:local_auth/local_auth.dart';
import 'omni_api.dart';
import 'device_identity.dart';

class ProjectItem {
  ProjectItem({
    required this.id,
    required this.name,
    required this.description,
    required this.ownerActor,
    required this.organizationId,
    required this.metadata,
    required this.archived,
    required this.createdAt,
    required this.updatedAt,
  });

  final String id;
  final String name;
  final String description;
  final String ownerActor;
  final String organizationId;
  final Map<String, dynamic> metadata;
  final bool archived;
  final DateTime createdAt;
  final DateTime updatedAt;
}

class AccountSettingItem {
  const AccountSettingItem(this.title, this.detail, this.action);

  final String title;
  final String detail;
  final String action;
}

const accountSettings = <AccountSettingItem>[
  AccountSettingItem('账户资料', '头像、名称、邮箱与移动端身份', '管理'),
  AccountSettingItem('工作区与组织', '团队、成员、权限与审批职责', '打开'),
  AccountSettingItem('自定义指令', '默认语气、工作习惯与项目偏好', '编辑'),
  AccountSettingItem('Skills / 工作流', '移动端任务模板、运行手册与自动化技能', '配置'),
  AccountSettingItem('连接器', 'GitHub、Google Drive、Slack、AWS 等外部应用', '连接'),
  AccountSettingItem('GitHub 仓库', '仓库访问、PR、分支、Review 与变更通知', '同步'),
  AccountSettingItem('执行环境', '本地、云端、worktree、终端与沙盒策略', '设置'),
  AccountSettingItem('Secrets / 环境变量', '令牌、密钥、环境变量与敏感凭据', '管理'),
  AccountSettingItem('通知', '审批、任务完成、失败、评论与提醒', '设置'),
  AccountSettingItem('外观', '主题、密度、语言、快捷键与侧栏显示', '调整'),
  AccountSettingItem('数据控制', '记忆、历史记录、导出、删除与隐私边界', '查看'),
  AccountSettingItem('安全与登录', '设备、会话、生物识别与退出登录', '检查'),
];

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
  final projectController = TextEditingController();
  final reasonController = TextEditingController(
    text: 'Approved from Omni Mobile with biometric/PIN confirmation',
  );
  Map<String, dynamic>? snapshot;
  String? chatConversationId;
  String chatProfile = 'fast';
  List<dynamic> chatMessages = <dynamic>[];
  List<ProjectItem> projects = <ProjectItem>[];
  String? activeProjectId;
  String projectError = '';
  String error = '';
  String securityState = 'secure storage ready';
  String pushState = 'push not registered';
  bool accountSettingsOpen = true;
  bool dailyAutomation = true;
  bool approvalAutomation = true;
  bool contentAutomation = false;
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

  @override
  void dispose() {
    gatewayController.dispose();
    tokenController.dispose();
    actorController.dispose();
    taskController.dispose();
    projectController.dispose();
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

  String _operationKey(String prefix) {
    return '$prefix-${DateTime.now().microsecondsSinceEpoch}-${Object().hashCode}';
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

  ProjectItem? get activeProject {
    for (final project in projects) {
      if (project.id == activeProjectId) return project;
    }
    return null;
  }

  String get activeProjectName => activeProject?.name ?? '未选择项目';

  DateTime _dateFromGateway(dynamic value) {
    if (value is num) {
      return DateTime.fromMillisecondsSinceEpoch((value * 1000).round());
    }
    if (value is String) {
      return DateTime.tryParse(value) ?? DateTime.now();
    }
    return DateTime.now();
  }

  ProjectItem _projectFromGateway(Map<String, dynamic> raw) {
    return ProjectItem(
      id: (raw['project_id'] ?? raw['id'] ?? '').toString(),
      name: (raw['name'] ?? 'Untitled project').toString(),
      description: (raw['description'] ?? '').toString(),
      ownerActor: (raw['owner_actor'] ?? '').toString(),
      organizationId: (raw['organization_id'] ?? '').toString(),
      metadata: raw['metadata'] is Map
          ? Map<String, dynamic>.from(raw['metadata'] as Map)
          : <String, dynamic>{},
      archived: raw['archived'] == true,
      createdAt: _dateFromGateway(raw['created_at'] ?? raw['createdAt']),
      updatedAt: _dateFromGateway(
        raw['updated_at'] ??
            raw['updatedAt'] ??
            raw['created_at'] ??
            raw['createdAt'],
      ),
    );
  }

  Future<List<ProjectItem>> syncProjects() async {
    final response = await client.projects();
    final rawProjects = response['projects'];
    final loaded = <ProjectItem>[];
    if (rawProjects is List) {
      for (final raw in rawProjects) {
        if (raw is Map) {
          final project = _projectFromGateway(Map<String, dynamic>.from(raw));
          if (project.id.isNotEmpty) loaded.add(project);
        }
      }
    }
    if (!mounted) return loaded;
    setState(() {
      projects = loaded;
      if (activeProjectId == null ||
          !loaded.any((project) => project.id == activeProjectId)) {
        activeProjectId = loaded.isEmpty ? null : loaded.first.id;
      }
    });
    return loaded;
  }

  Future<void> createProject([String? fallbackName]) async {
    final name = (fallbackName ?? projectController.text).trim();
    if (name.isEmpty) {
      setState(() => projectError = '请输入项目名称。');
      return;
    }
    if (projects.any(
      (project) => project.name.toLowerCase() == name.toLowerCase(),
    )) {
      setState(() => projectError = '项目已存在。');
      return;
    }
    try {
      final identity = deviceIdentity ?? await identityStore.loadOrCreate();
      deviceIdentity = identity;
      final response = await client.createProject(
        name,
        sourceDeviceId: identity.deviceId,
        idempotencyKey: _operationKey('mobile-project-create'),
      );
      final rawProject = response['project'];
      if (rawProject is! Map) throw Exception('Gateway did not return project');
      final project = _projectFromGateway(
        Map<String, dynamic>.from(rawProject),
      );
      setState(() {
        projects = <ProjectItem>[
          project,
          ...projects.where((item) => item.id != project.id),
        ];
        activeProjectId = project.id;
        projectController.clear();
        projectError = '';
      });
    } catch (e) {
      setState(() => projectError = e.toString());
    }
  }

  Future<void> mutateActiveProject(String action) async {
    final project = activeProject;
    if (project == null) return;
    try {
      if (action == 'delete') {
        await client.deleteProject(
          project.id,
          idempotencyKey: _operationKey('mobile-project-delete'),
        );
      } else {
        await client.updateProject(
          project.id,
          name: action == 'rename' ? '${project.name} (updated)' : null,
          archived: action == 'archive' ? !project.archived : null,
          idempotencyKey: _operationKey('mobile-project-$action'),
        );
      }
      await syncProjects();
      if (mounted) setState(() => projectError = '');
    } catch (e) {
      if (mounted) setState(() => projectError = e.toString());
    }
  }

  void applyPrompt(String prompt) {
    final project = activeProject;
    taskController.text = project == null
        ? prompt
        : '[${project.name}] $prompt';
    setState(() {});
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
      await syncProjects();
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
        snapshot?['pending_approvals'] as List<dynamic>? ?? <dynamic>[];
    final notifications =
        snapshot?['notifications'] as List<dynamic>? ?? <dynamic>[];
    return MaterialApp(
      title: 'Omni Mobile',
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        colorScheme: ColorScheme.fromSeed(
          seedColor: Colors.indigo,
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF07101B),
        cardTheme: CardThemeData(
          color: const Color(0xFF101A2A),
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(22),
          ),
        ),
      ),
      home: Scaffold(
        appBar: AppBar(
          title: const Text('AI 助理 Mobile'),
          actions: <Widget>[
            IconButton(
              tooltip: '账户设置',
              icon: const Icon(Icons.account_circle_outlined),
              onPressed: () =>
                  setState(() => accountSettingsOpen = !accountSettingsOpen),
            ),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: <Widget>[
            _heroCard(),
            _projectCard(),
            _quickActions(),
            _composerCard(),
            if (error.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  error,
                  style: const TextStyle(color: Colors.redAccent),
                ),
              ),
            if (accountSettingsOpen) _accountSettingsCard(),
            _connectionCard(),
            _approvalCard(approvals),
            _automationCard(),
            _messagesCard(),
            _notificationsCard(notifications),
          ],
        ),
      ),
    );
  }

  Widget _heroCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Container(
                  width: 44,
                  height: 44,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(16),
                    gradient: const LinearGradient(
                      colors: <Color>[Color(0xFF6674FF), Color(0xFF5DCAFF)],
                    ),
                  ),
                  child: const Text(
                    'AI',
                    style: TextStyle(fontWeight: FontWeight.w800),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    '我们应该在 AI 助理中做些什么？',
                    style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              '智能协作 · 远程审批 · 移动问答 · Gateway 项目同步',
              style: TextStyle(color: Colors.blueGrey.shade100),
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: <Widget>[
                Chip(label: Text('Security: $securityState')),
                Chip(label: Text('Push: $pushState')),
                Chip(
                  label: Text(
                    'Device: ${deviceIdentity?.deviceId ?? 'not enrolled'}',
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _projectCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                const Expanded(
                  child: Text(
                    '项目',
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                  ),
                ),
                OutlinedButton(
                  onPressed: () {
                    syncProjects();
                  },
                  child: const Text('同步'),
                ),
                const SizedBox(width: 8),
                FilledButton.tonal(
                  onPressed: () {
                    createProject(
                      projectController.text.isEmpty ? '新项目' : null,
                    );
                  },
                  child: const Text('＋ 新建项目'),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Row(
              children: <Widget>[
                Expanded(
                  child: TextField(
                    controller: projectController,
                    decoration: const InputDecoration(
                      labelText: '输入项目名称后创建',
                      border: OutlineInputBorder(),
                    ),
                    onSubmitted: (_) {
                      createProject();
                    },
                  ),
                ),
                const SizedBox(width: 10),
                FilledButton(
                  onPressed: () {
                    createProject();
                  },
                  child: const Text('创建'),
                ),
              ],
            ),
            if (projectError.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  projectError,
                  style: const TextStyle(color: Colors.orangeAccent),
                ),
              ),
            const SizedBox(height: 12),
            if (projects.isEmpty)
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(color: Colors.white24),
                ),
                child: const Text(
                  '暂无项目。项目由 Gateway 创建并跨 Web Admin / Desktop / Mobile 同步。',
                ),
              )
            else
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: <Widget>[
                  for (final project in projects)
                    ChoiceChip(
                      selected: project.id == activeProjectId,
                      label: Text(project.name),
                      onSelected: (_) =>
                          setState(() => activeProjectId = project.id),
                    ),
                ],
              ),
            if (activeProject != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Wrap(
                  spacing: 8,
                  children: <Widget>[
                    OutlinedButton(
                      onPressed: () {
                        mutateActiveProject('rename');
                      },
                      child: const Text('重命名'),
                    ),
                    OutlinedButton(
                      onPressed: () {
                        mutateActiveProject('archive');
                      },
                      child: Text(activeProject!.archived ? '恢复' : '归档'),
                    ),
                    OutlinedButton(
                      onPressed: () {
                        mutateActiveProject('delete');
                      },
                      child: const Text('删除'),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _quickActions() {
    final actions = <Map<String, String>>[
      <String, String>{
        'title': '远程审批',
        'detail': '查看并确认高风险动作',
        'prompt': '请检查当前待审批动作，并说明是否可以批准。',
      },
      <String, String>{
        'title': '移动问答',
        'detail': '随时追问项目状态',
        'prompt': '汇总当前项目状态、风险点和下一步动作。',
      },
      <String, String>{
        'title': '任务派发',
        'detail': '发送到桌面运行器执行',
        'prompt': '把这个任务拆分为可由桌面端执行的步骤。',
      },
      <String, String>{
        'title': '连接应用',
        'detail': '同步 GitHub / AWS / Drive',
        'prompt': '检查当前工作流需要连接哪些应用，并列出权限边界。',
      },
    ];
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            const Text(
              '快捷功能',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 12),
            for (final action in actions)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.auto_awesome_outlined),
                title: Text(action['title']!),
                subtitle: Text(action['detail']!),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => applyPrompt(action['prompt']!),
              ),
          ],
        ),
      ),
    );
  }

  Widget _composerCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: Text(
                    '当前项目：$activeProjectName',
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ),
                DropdownButton<String>(
                  value: chatProfile,
                  items: const <DropdownMenuItem<String>>[
                    DropdownMenuItem<String>(value: 'fast', child: Text('快速')),
                    DropdownMenuItem<String>(
                      value: 'planner',
                      child: Text('规划'),
                    ),
                    DropdownMenuItem<String>(value: 'local', child: Text('本地')),
                  ],
                  onChanged: (value) =>
                      setState(() => chatProfile = value ?? 'fast'),
                ),
              ],
            ),
            TextField(
              controller: taskController,
              maxLines: 3,
              decoration: InputDecoration(
                labelText: activeProject == null
                    ? '先创建项目，或直接向 AI 助理提问'
                    : '在 ${activeProject!.name} 中输入任务',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: <Widget>[
                FilledButton.icon(
                  onPressed: () {
                    askAssistant();
                  },
                  icon: const Icon(Icons.arrow_upward),
                  label: const Text('问一下 AI'),
                ),
                OutlinedButton.icon(
                  onPressed: () {
                    sendTask();
                  },
                  icon: const Icon(Icons.desktop_windows_outlined),
                  label: const Text('发送并请求桌面执行'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _accountSettingsCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                const Expanded(
                  child: Text(
                    '账户设置',
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                  ),
                ),
                Chip(
                  label: const Text('Codex-style'),
                  backgroundColor: Colors.indigo.withValues(alpha: .22),
                ),
              ],
            ),
            const SizedBox(height: 8),
            for (final setting in accountSettings)
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(setting.title),
                subtitle: Text(setting.detail),
                trailing: const Text('未启用'),
                enabled: false,
                onTap: null,
              ),
          ],
        ),
      ),
    );
  }

  Widget _connectionCard() {
    return Card(
      child: ExpansionTile(
        initiallyExpanded: false,
        title: const Text('连接配置'),
        subtitle: const Text('Gateway、Token、Actor 与设备注册'),
        childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        children: <Widget>[
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
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              onPressed: () {
                connect();
              },
              child: const Text('连接 Omni Gateway'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _approvalCard(List<dynamic> approvals) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
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
            const SizedBox(height: 12),
            if (approvals.isEmpty)
              const Text('暂无来自 Gateway 的待审批动作。')
            else
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
          ],
        ),
      ),
    );
  }

  Widget _automationCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            const Text(
              '自动化',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
            ),
            SwitchListTile(
              value: dailyAutomation,
              onChanged: (value) => setState(() => dailyAutomation = value),
              title: const Text('每日数据采集'),
            ),
            SwitchListTile(
              value: approvalAutomation,
              onChanged: (value) => setState(() => approvalAutomation = value),
              title: const Text('审批提醒'),
            ),
            SwitchListTile(
              value: contentAutomation,
              onChanged: (value) => setState(() => contentAutomation = value),
              title: const Text('内容发布流程'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _messagesCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            const Text(
              '最近对话',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
            ),
            if (chatMessages.isEmpty)
              const Text('暂无对话。')
            else
              for (final message in chatMessages.take(8))
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text('${message['role'] ?? 'message'}'),
                  subtitle: Text(
                    '${message['content'] ?? ''}\n${message['model_provider'] ?? ''} ${message['model_name'] ?? ''} ${message['trace_id'] ?? ''}',
                  ),
                  isThreeLine: true,
                ),
          ],
        ),
      ),
    );
  }

  Widget _notificationsCard(List<dynamic> notifications) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(
              '通知：${notifications.length}',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            if (notifications.isEmpty)
              const Text('暂无通知。')
            else
              for (final item in notifications.take(8))
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(item['title']?.toString() ?? ''),
                  subtitle: Text(item['body']?.toString() ?? ''),
                ),
          ],
        ),
      ),
    );
  }
}
