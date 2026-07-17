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
