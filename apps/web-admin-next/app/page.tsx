'use client';

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { OmniAdminApi } from '../lib/api';
import type { AdminRole, WebAdminDeviceRegistration } from '../lib/api';
import {
  loadOrCreateWebAdminIdentity,
  signWebAdminChallenge,
  signWebAdminDeviceRequest,
} from '../lib/device-identity';

const ROLE_HELP: Record<AdminRole, string> = {
  viewer: '只读查看设备、运行器、通知与审计状态。',
  operator: '可发起普通操作并刷新运行状态。',
  owner: '可审批/拒绝高风险动作。'
};

type Tone = 'green' | 'blue' | 'orange' | 'gray' | 'red' | 'violet';

type ProjectItem = {
  id: string;
  name: string;
  createdAt: string;
};

type QuickAction = {
  key: string;
  icon: string;
  title: string;
  subtitle: string;
  prompt: string;
  tone: Tone;
};

type SuggestedTask = {
  icon: string;
  title: string;
  tag: string;
  time: string;
  prompt: string;
  tone: Tone;
};

type AccountSetting = {
  title: string;
  detail: string;
  action: string;
};

type GatewayRecord = Record<string, any>;
type ConnectionState = { status: string; tone: Tone; detail: string };

type CompactTaskRow = readonly [title: string, status: string, tone: Tone];

const QUICK_ACTIONS: QuickAction[] = [
  {
    key: 'code',
    icon: '</>',
    title: '代码协作',
    subtitle: '编写、调试与审查\n代码更高效',
    prompt: '帮我检查当前代码分支，拆分高风险变更，并生成可审查的 PR 计划。',
    tone: 'blue',
  },
  {
    key: 'data',
    icon: '▰',
    title: '数据采集',
    subtitle: '连接数据源，采集\n并清洗数据',
    prompt: '帮我生成今天的数据采集日报，并列出异常数据、缺失字段和后续动作。',
    tone: 'green',
  },
  {
    key: 'creator',
    icon: '●',
    title: '达人运营',
    subtitle: '洞察达人数据，优\n化投放策略',
    prompt: '分析达人投放数据，按照 BAD、NORMAL、AI good、AI bad、GOOD 归类并给出建议。',
    tone: 'violet',
  },
  {
    key: 'content',
    icon: '✎',
    title: '内容生成',
    subtitle: '生成文案、脚本与\n多媒体内容',
    prompt: '为当前产品生成 TikTok 内容脚本，口播要自然，不要太硬带货。',
    tone: 'orange',
  },
];

const SUGGESTED_TASKS: SuggestedTask[] = [
  {
    icon: '⌁',
    title: '拆分混合分支为知识升级 PR',
    tag: '代码协作',
    time: '刚刚',
    prompt: '拆分混合分支为知识升级 PR，并保留可回滚路径。',
    tone: 'blue',
  },
  {
    icon: '☁',
    title: '补齐 AWS Device Farm 证据',
    tag: '测试与运维',
    time: '1 小时前',
    prompt: '补齐 AWS Device Farm 真机测试证据，并整理成 Real GA evidence checklist。',
    tone: 'green',
  },
  {
    icon: '▯',
    title: '区分真机修复与脚手架变更',
    tag: '研发流程',
    time: '3 小时前',
    prompt: '区分真实 iPhone 修复与 regenerated Flutter scaffold noise，输出可审查 diff 说明。',
    tone: 'green',
  },
  {
    icon: '⛓',
    title: '连接常用应用到工作流',
    tag: '自动化',
    time: '5 小时前',
    prompt: '把 GitHub、AWS、Google Drive、Slack 连接到 AI 助理工作流，并设计审批边界。',
    tone: 'blue',
  },
];

const ACCOUNT_SETTINGS: AccountSetting[] = [
  { title: '账户资料', detail: '头像、名称、邮箱与会话身份', action: '管理' },
  { title: '工作区与组织', detail: '个人工作区、团队、成员与权限', action: '打开' },
  { title: '自定义指令', detail: '默认语气、工作习惯与项目偏好', action: '编辑' },
  { title: 'Skills / 工作流', detail: '复用任务模板、运行手册与自动化技能', action: '配置' },
  { title: '连接器', detail: 'GitHub、Google Drive、Slack、AWS 等外部应用', action: '连接' },
  { title: 'GitHub 仓库', detail: '仓库访问、PR 策略、分支与代码审查权限', action: '同步' },
  { title: '执行环境', detail: '本地、云端、worktree、终端与沙盒策略', action: '设置' },
  { title: 'Secrets / 环境变量', detail: '令牌、密钥、环境变量与敏感凭据', action: '管理' },
  { title: '通知', detail: '审批、任务完成、失败、评论与提醒', action: '设置' },
  { title: '外观', detail: '主题、密度、语言、快捷键与侧栏显示', action: '调整' },
  { title: '数据控制', detail: '记忆、历史记录、导出、删除与隐私边界', action: '查看' },
  { title: '安全与登录', detail: '设备、会话、二次验证与退出登录', action: '检查' },
];

const RECENT_TASKS: CompactTaskRow[] = [
  ['数据采集日报', '已完成', 'green'],
  ['内容生成：产品脚本', '进行中', 'blue'],
  ['达人投放分析报告', '进行中', 'blue'],
  ['API 接口联调', '已完成', 'green'],
];

const INTEGRATION_NAMES = ['GitHub', 'AWS', 'Google Drive', 'Slack'] as const;

function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`status-pill tone-${tone}`}>{children}</span>;
}

function IconButton({ children, label, onClick }: { children: ReactNode; label: string; onClick?: () => void }) {
  return <button className="icon-button" type="button" aria-label={label} onClick={onClick}>{children}</button>;
}

function initialsFromProject(name: string) {
  const trimmed = name.trim();
  if (!trimmed) return 'P';
  return trimmed.slice(0, 2).toUpperCase();
}

function initialsFromActor(value: string) {
  const compact = value.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]+/g, '').trim();
  return (compact || 'WA').slice(0, 2).toUpperCase();
}

function formatDate(value: unknown) {
  if (!value) return 'n/a';
  try {
    const date = new Date(String(value));
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  } catch {
    return String(value);
  }
}

function textFrom(value: unknown, fallback = 'n/a') {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

function asRecordArray(value: unknown): GatewayRecord[] {
  return Array.isArray(value) ? value.filter((item): item is GatewayRecord => !!item && typeof item === 'object') : [];
}

export default function Page() {
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:18789');
  const [token, setToken] = useState('');
  const [actor, setActor] = useState('web-admin');
  const [role, setRole] = useState<AdminRole>('viewer');
  const [csrfToken, setCsrfToken] = useState('');
  const [deviceIdentity, setDeviceIdentity] = useState<WebAdminDeviceRegistration | null>(null);
  const [decisionReason, setDecisionReason] = useState('Reviewed in Web Admin controlled-staging console');
  const [snapshot, setSnapshot] = useState<GatewayRecord | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<GatewayRecord | null>(null);
  const [ecosystem, setEcosystem] = useState<GatewayRecord | null>(null);
  const [chatConversationId, setChatConversationId] = useState('');
  const [chatInput, setChatInput] = useState('帮我分析今天任务状态');
  const [chatProfile, setChatProfile] = useState('fast');
  const [chatMessages, setChatMessages] = useState<GatewayRecord[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [activeProjectId, setActiveProjectId] = useState('');
  const [newProjectName, setNewProjectName] = useState('');
  const [projectError, setProjectError] = useState('');
  const [globalSearch, setGlobalSearch] = useState('');
  const [automationDaily, setAutomationDaily] = useState(true);
  const [automationPr, setAutomationPr] = useState(true);
  const [automationPublish, setAutomationPublish] = useState(false);
  const [accountSettingsOpen, setAccountSettingsOpen] = useState(true);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const canApprove = role === 'owner';
  const canAsk = role === 'operator' || role === 'owner';

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        document.getElementById('global-search')?.focus();
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  async function establishSession() {
    const response = await fetch('/api/session/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ gatewayUrl: baseUrl, token, actor, role })
    });
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json() as GatewayRecord;
    const verifiedRole = (body.role || role) as AdminRole;
    const verifiedActor = String(body.actor || actor);
    setCsrfToken(String(body.csrfToken || ''));
    setActor(verifiedActor);
    setRole(verifiedRole);
    return {
      csrfToken: String(body.csrfToken || ''),
      actor: verifiedActor,
      role: verifiedRole,
    };
  }

  function webAdminApiFor(
    identity: WebAdminDeviceRegistration,
    activeCsrf = csrfToken,
    activeActor = actor,
    activeRole = role,
  ) {
    return new OmniAdminApi({
      csrfToken: activeCsrf,
      actor: activeActor,
      role: activeRole,
      deviceId: identity.deviceId,
      publicKeyPem: identity.publicKeyPem,
      deviceSigner: signWebAdminDeviceRequest,
    });
  }

  function randomPairingCode() {
    const bytes = new Uint8Array(18);
    crypto.getRandomValues(bytes);
    return `web-${Array.from(bytes).map((value) => value.toString(16).padStart(2, '0')).join('')}`;
  }

  async function ensureWebAdminDevice(
    activeCsrf = csrfToken,
    activeActor = actor,
    activeRole = role,
  ) {
    const identity = await loadOrCreateWebAdminIdentity();
    setDeviceIdentity(identity);
    const activeApi = webAdminApiFor(identity, activeCsrf, activeActor, activeRole);
    await activeApi.registerAdminDevice(identity);

    const pairingCode = randomPairingCode();
    const started = await activeApi.startDeviceEnrollment('web_admin', pairingCode);
    const enrollmentId = String(started.enrollment.enrollment_id);
    await activeApi.completeDeviceEnrollment(enrollmentId, pairingCode, identity);
    const challenged = await activeApi.issueDeviceChallenge(enrollmentId, identity.deviceId);
    const challenge = challenged.challenge;
    const signature = await signWebAdminChallenge(String(challenge.signing_message));
    await activeApi.verifyDeviceChallenge(
      enrollmentId,
      String(challenge.challenge_id),
      identity.deviceId,
      signature,
    );
    return identity;
  }

  async function load() {
    setError('');
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      setSnapshot(await activeApi.bootstrap());
      setRuntimeStatus(await activeApi.runtime());
      setEcosystem(await activeApi.ecosystem());
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function decide(id: string, decision: 'approved' | 'rejected') {
    if (!canApprove) {
      setError('当前角色不是 owner，不能执行审批。');
      return;
    }
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      await activeApi.decide(id, decision, decisionReason);
      setSnapshot(await activeApi.bootstrap());
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function askAssistant() {
    if (!canAsk) {
      setError('当前角色不是 operator/owner，不能发起模型问答。');
      return;
    }
    const content = chatInput.trim();
    if (!content) return;
    setError('');
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const activeApi = new OmniAdminApi({ csrfToken: session.csrfToken, actor: session.actor, role: session.role });
      let conversationId = chatConversationId;
      if (!conversationId) {
        const created = await activeApi.createConversation('Web Admin Chat');
        conversationId = String(created.conversation.conversation_id);
        setChatConversationId(conversationId);
      }
      const result = await activeApi.askConversation(conversationId, content, chatProfile);
      const messages = (await activeApi.listMessages(conversationId)).messages;
      setChatMessages(asRecordArray(messages).length > 0 ? asRecordArray(messages) : asRecordArray([result.user_message, result.assistant_message]));
      setSnapshot(await activeApi.bootstrap());
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  function createProject(projectName = newProjectName) {
    const name = projectName.trim();
    if (!name) {
      setProjectError('请输入项目名称。');
      return;
    }
    if (projects.some((project) => project.name.toLowerCase() === name.toLowerCase())) {
      setProjectError('项目已存在。');
      return;
    }
    const project: ProjectItem = {
      id: `${Date.now()}-${name.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-')}`,
      name,
      createdAt: new Date().toISOString(),
    };
    setProjects((current) => [project, ...current]);
    setActiveProjectId(project.id);
    setNewProjectName('');
    setProjectError('');
  }

  const activeProject = projects.find((project) => project.id === activeProjectId) || null;
  const activeProjectName = activeProject?.name || '未选择项目';
  const approvals = asRecordArray(snapshot?.pending_approvals);
  const notifications = asRecordArray(snapshot?.notifications);
  const devices = asRecordArray(snapshot?.devices);
  const runtime = (runtimeStatus?.runtime || snapshot?.runtime_status || {}) as GatewayRecord;
  const channelCatalog = asRecordArray(ecosystem?.channels);
  const liveProjectStatus = csrfToken ? '进行中' : '未连接';
  const actorInitials = initialsFromActor(actor);
  const releaseEvidence = runtime.release_evidence || runtime.ga_evidence || runtimeStatus?.release_evidence || runtimeStatus?.ga_evidence || snapshot?.release_evidence || snapshot?.ga_evidence || null;
  const releaseEvidenceText = releaseEvidence ? (JSON.stringify(releaseEvidence, null, 2) || String(releaseEvidence)) : '尚未加载 release_evidence / GA evidence。';

  function pickPrompt(prompt: string) {
    setChatInput(activeProject ? `[${activeProject.name}] ${prompt}` : prompt);
  }

  function connectionStatus(name: string): ConnectionState {
    const lowerName = name.toLowerCase();
    const channel = channelCatalog.find((item) => {
      const raw = `${item.name || ''} ${item.id || ''} ${item.channel || ''} ${item.provider || ''}`.toLowerCase();
      return raw.includes(lowerName) || (!!raw.trim() && lowerName.includes(raw.trim()));
    });
    if (channel?.connected === true || channel?.authorized === true) {
      return { status: '已连接', tone: 'green', detail: '已由后端连接状态确认' };
    }
    if (channel?.enabled === true) {
      return { status: '待授权', tone: 'orange', detail: '通道已启用，但未确认账户授权' };
    }
    return { status: '待配置', tone: 'gray', detail: '未从后端连接状态确认授权' };
  }

  return <main className="app-shell">
    <aside className="sidebar left-sidebar" aria-label="Primary navigation">
      <div className="window-row">
        <span className="traffic red" />
        <span className="traffic yellow" />
        <span className="traffic green" />
        <button className="sidebar-toggle" type="button" aria-label="折叠侧栏">▣</button>
      </div>

      <nav className="primary-nav">
        <button className="nav-item nav-primary" type="button" onClick={() => setChatInput('')}>新对话 <span>＋</span></button>
        <button className="nav-item" type="button">⌕ <span>搜索</span></button>
        <button className="nav-item" type="button">◴ <span>已安排</span></button>
        <button className="nav-item" type="button">✣ <span>插件</span></button>
      </nav>

      <section className="project-box">
        <div className="section-title"><span>项目</span><button type="button" onClick={() => createProject(newProjectName || '新项目')}>＋ 新建项目</button></div>
        <form className="project-create-form" onSubmit={(event) => { event.preventDefault(); createProject(); }}>
          <input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} placeholder="输入项目名称后创建" />
          <button type="submit">创建</button>
        </form>
        {projectError && <p className="project-error">{projectError}</p>}
        <div className="project-list">
          {projects.length === 0 ? <div className="project-empty-state">
            <strong>暂无项目</strong>
            <span>项目内容需要由用户自行创建。创建后才会进入左侧项目列表、顶部工作区和右侧项目卡片。</span>
          </div> : projects.map((project) => <button
            className={`project-row ${project.id === activeProjectId ? 'active' : ''}`}
            key={project.id}
            type="button"
            onClick={() => setActiveProjectId(project.id)}
          >
            <span className="project-icon">{initialsFromProject(project.name)}</span>
            <span>{project.name}</span>
            {project.id === activeProjectId ? <span className="row-more">...</span> : <span className="project-star">☆</span>}
          </button>)}
        </div>
      </section>

      <button className="profile-card profile-button" type="button" onClick={() => setAccountSettingsOpen((open) => !open)}>
        <div className="avatar">{actorInitials}</div>
        <div>
          <strong>{actor}</strong>
          <small>{role} · Web Admin</small>
        </div>
        <span>{accountSettingsOpen ? '⌃' : '⌄'}</span>
      </button>
    </aside>

    <section className="workspace">
      <header className="topbar">
        <button className="workspace-switcher" type="button">▣ <span>{activeProjectName}</span>⌄</button>
        <label className="global-search" htmlFor="global-search">
          <span>⌕</span>
          <input
            id="global-search"
            value={globalSearch}
            onChange={(event) => setGlobalSearch(event.target.value)}
            placeholder="全局搜索  ⌘K"
          />
        </label>
        <button className="connect-button" type="button" onClick={load} disabled={loading}>▦ 连接应用</button>
        <IconButton label="通知">◔<span className="notify-dot" /></IconButton>
        <button className="top-avatar" type="button" aria-label="打开账户设置" onClick={() => setAccountSettingsOpen((open) => !open)}>{actorInitials}⌄</button>
      </header>

      <div className="hero-panel">
        <div className="orb orb-one" />
        <div className="orb orb-two" />
        <div className="hero-copy">
          <h1>我们应该在 AI 助理中做些什么？</h1>
          <p>✦ 智能协作 · 深度思考 · 高效交付</p>
        </div>

        <section className="composer-card" aria-label="AI assistant composer">
          <textarea
            value={chatInput}
            onChange={(event) => setChatInput(event.target.value)}
            placeholder={activeProject ? `在 ${activeProject.name} 中输入任务...` : '先创建项目，或直接向 AI 助理提问...'}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                void askAssistant();
              }
            }}
          />
          <div className="composer-actions">
            <div className="composer-left">
              <IconButton label="添加附件">＋</IconButton>
              <button className="mode-select" type="button">⚙ 自定义⌄</button>
            </div>
            <div className="composer-right">
              <select value={chatProfile} onChange={(event) => setChatProfile(event.target.value)} aria-label="选择模型配置">
                <option value="fast">快速</option>
                <option value="planner">规划</option>
                <option value="local">本地</option>
              </select>
              <IconButton label="语音输入">◌</IconButton>
              <button className="send-button" type="button" onClick={askAssistant} disabled={!canAsk || loading}>↑</button>
            </div>
          </div>
        </section>

        {error && <div className="error-banner">{error}</div>}
      </div>

      <section className="side-card" style={{ maxWidth: 900, margin: '0 auto 24px' }}>
        <div className="card-title"><h3>Gateway 连接配置</h3><StatusPill tone={csrfToken ? 'green' : 'gray'}>{csrfToken ? 'session ready' : '未建立 session'}</StatusPill></div>
        <details className="connection-details" open>
          <summary>Gateway URL / Token / Actor / Role</summary>
          <label>Gateway URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
          <label>Session Token<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
          <label>Actor<input value={actor} onChange={(event) => setActor(event.target.value)} /></label>
          <label>Role
            <select value={role} onChange={(event) => setRole(event.target.value as AdminRole)}>
              <option value="viewer">viewer</option>
              <option value="operator">operator</option>
              <option value="owner">owner</option>
            </select>
          </label>
          <p className="hint-text">{ROLE_HELP[role]}</p>
          <button className="secondary-action" type="button" onClick={() => { void establishSession(); }}>建立 Web Session / CSRF</button>
        </details>
      </section>

      <section className="quick-grid">
        {QUICK_ACTIONS.map((action) => <button
          key={action.key}
          className={`quick-card tone-card-${action.tone}`}
          type="button"
          onClick={() => pickPrompt(action.prompt)}
        >
          <span className="quick-icon">{action.icon}</span>
          <strong>{action.title}</strong>
          <small>{action.subtitle}</small>
          <span className="card-arrow">→</span>
        </button>)}
      </section>

      <section className="suggestion-section">
        <div className="list-header"><h2>建议任务</h2><button type="button">查看全部 ›</button></div>
        <div className="task-list">
          {SUGGESTED_TASKS.map((task) => <button
            className="task-row"
            key={task.title}
            type="button"
            onClick={() => pickPrompt(task.prompt)}
          >
            <span className={`task-icon tone-${task.tone}`}>{task.icon}</span>
            <strong>{task.title}</strong>
            <span className="task-tag">{task.tag}</span>
            <small>{task.time}</small>
            <span>›</span>
          </button>)}
        </div>
      </section>

      {chatMessages.length > 0 && <section className="conversation-preview">
        <div className="list-header"><h2>完整对话记录</h2><span>{chatMessages.length} 条</span></div>
        {chatMessages.map((message, index) => <article className="message-card" key={`${message.role || 'message'}-${index}`}>
          <strong>{message.role === 'assistant' ? 'AI 助理' : '你'}</strong>
          <p>{String(message.content || '')}</p>
          <small>{message.model_provider || message.model_name || message.model_profile || 'workspace'}</small>
          {message.trace_id && <small>audit_trace_id: {message.trace_id}</small>}
        </article>)}
      </section>}
    </section>

    <aside className="sidebar right-sidebar" aria-label="Context panel">
      {accountSettingsOpen && <section className="side-card account-settings-card">
        <div className="card-title"><h3>账户设置</h3><StatusPill tone="blue">Codex-style</StatusPill></div>
        <div className="account-head"><span className="avatar">{actorInitials}</span><div><strong>{actor}</strong><small>{role} · 当前工作区</small></div></div>
        <div className="settings-grid">
          {ACCOUNT_SETTINGS.map((setting) => <button className="setting-row" key={setting.title} type="button">
            <span><strong>{setting.title}</strong><small>{setting.detail}</small></span>
            <em>{setting.action}</em>
          </button>)}
        </div>
      </section>}

      <section className="side-card current-project-card">
        <div className="card-title"><h3>当前项目</h3><StatusPill tone={activeProject ? (csrfToken ? 'green' : 'gray') : 'orange'}>{activeProject ? liveProjectStatus : '待创建'}</StatusPill></div>
        <div className="project-heading">▣ <strong>{activeProjectName}</strong></div>
        <small className="muted-label">项目描述</small>
        <p>{activeProject ? '用户创建的 AI 助理工作项目，可绑定数据、任务、审批与应用连接。' : '尚未创建项目。请在左侧输入项目名称并点击创建。'}</p>
        <small className="muted-label">负责人</small>
        <div className="owner-row"><span className="mini-avatar">{actorInitials}</span><span>{actor}</span></div>
        <small className="muted-label">成员</small>
        <div className="member-stack"><span>{actorInitials.slice(0, 1)}</span>{activeProject ? <em>+0</em> : <em>未配置</em>}</div>
        <button className="secondary-action" type="button" onClick={load} disabled={loading}>⚙ 项目设置 / 加载后台</button>
        <details className="connection-details">
          <summary>连接配置</summary>
          <label>Gateway URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
          <label>Session Token<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
          <label>Actor<input value={actor} onChange={(event) => setActor(event.target.value)} /></label>
          <label>Role
            <select value={role} onChange={(event) => setRole(event.target.value as AdminRole)}>
              <option value="viewer">viewer</option>
              <option value="operator">operator</option>
              <option value="owner">owner</option>
            </select>
          </label>
          <p className="hint-text">{ROLE_HELP[role]}</p>
          <button className="secondary-action" type="button" onClick={() => { void establishSession(); }}>建立 Web Session / CSRF</button>
        </details>
      </section>

      <section className="side-card">
        <div className="card-title"><h3>连接应用</h3><button type="button">管理</button></div>
        {INTEGRATION_NAMES.map((name) => {
          const connection = connectionStatus(name);
          return <div className="app-row" key={name}>
            <span className="app-mark">{name.slice(0, 1)}</span>
            <strong>{name}</strong>
            <StatusPill tone={connection.tone}>{connection.status}</StatusPill>
            <small>{connection.detail}</small>
          </div>;
        })}
      </section>

      <section className="side-card">
        <div className="card-title"><h3>最近任务</h3><button type="button">查看全部›</button></div>
        {RECENT_TASKS.map(([title, status, tone]) => <div className="compact-row" key={title}>
          <span>▣</span><strong>{title}</strong><StatusPill tone={tone}>{status}</StatusPill>
        </div>)}
      </section>

      <section className="side-card">
        <div className="card-title"><h3>审批状态</h3><span>{approvals.length} 项</span></div>
        {approvals.length === 0 ? <div className="project-empty-state">
          <strong>暂无待审批动作</strong>
          <span>Gateway 未返回 pending approvals；不会展示模拟审批或虚构 PR 状态。</span>
        </div> : approvals.map((approval) => {
          const approvalId = String(approval.approval_id || approval.id || '');
          return <div className="approval-row" key={approvalId || String(approval.action || approval.reason || 'approval')}>
            <div>
              <strong>{textFrom(approval.action, 'Approval')}</strong>
              <small>原因：{textFrom(approval.reason)}</small>
              <small>风险：{textFrom(approval.risk)} · 请求人：{textFrom(approval.requested_by || approval.actor || approval.source_actor)} · 过期：{formatDate(approval.expires_at)}</small>
            </div>
            <div className="decision-buttons">
              <button type="button" disabled={!canApprove || loading || !approvalId} onClick={() => decide(approvalId, 'approved')}>批准</button>
              <button type="button" disabled={!canApprove || loading || !approvalId} onClick={() => decide(approvalId, 'rejected')}>拒绝</button>
            </div>
          </div>;
        })}
        <label className="audit-reason">审批原因<textarea value={decisionReason} onChange={(event) => setDecisionReason(event.target.value)} /></label>
      </section>

      <section className="side-card automation-card">
        <div className="card-title"><h3>自动化</h3><button type="button">查看全部›</button></div>
        <label className="toggle-row"><span>⚙ 每日数据采集</span><input type="checkbox" checked={automationDaily} onChange={(event) => setAutomationDaily(event.target.checked)} /></label>
        <label className="toggle-row"><span>☑ PR 自动检查</span><input type="checkbox" checked={automationPr} onChange={(event) => setAutomationPr(event.target.checked)} /></label>
        <label className="toggle-row"><span>▣ 内容发布流程</span><input type="checkbox" checked={automationPublish} onChange={(event) => setAutomationPublish(event.target.checked)} /></label>
      </section>

      <section className="side-card telemetry-card">
        <div className="stat-line"><span>项目</span><strong>{projects.length}</strong></div>
        <div className="stat-line"><span>设备</span><strong>{devices.length}</strong></div>
        <div className="stat-line"><span>通知</span><strong>{notifications.length}</strong></div>
        <div className="stat-line"><span>Runtime</span><strong>{runtime.resource_guard ? 'online' : 'standby'}</strong></div>
      </section>

      <section className="side-card">
        <div className="card-title"><h3>GA / Release Evidence</h3><StatusPill tone={releaseEvidence ? 'green' : 'gray'}>{releaseEvidence ? 'loaded' : 'not loaded'}</StatusPill></div>
        <pre>{releaseEvidenceText}</pre>
      </section>

      <section className="side-card">
        <div className="card-title"><h3>通知详情</h3><span>{notifications.length} 条</span></div>
        {notifications.length === 0 ? <div className="project-empty-state"><strong>暂无通知</strong><span>Gateway 未返回 warning/error notification。</span></div> : notifications.map((item, index) => <div className="compact-row" key={`${item.id || item.title || 'notification'}-${index}`}>
          <span>◔</span>
          <strong>{textFrom(item.title || item.type, 'Notification')}</strong>
          <small>{textFrom(item.body || item.message || item.detail)}</small>
        </div>)}
      </section>
    </aside>
  </main>;
}
