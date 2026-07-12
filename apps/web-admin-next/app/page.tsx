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
  owner: '可审批/拒绝高风险动作。',
};

type Tone = 'green' | 'blue' | 'orange' | 'gray' | 'red' | 'violet';

type ProjectItem = {
  id: string;
  name: string;
  description: string;
  ownerActor: string;
  organizationId: string;
  metadata: GatewayRecord;
  archived: boolean;
  createdAt: string;
  updatedAt: string;
};

type QuickAction = {
  key: string;
  icon: string;
  title: string;
  subtitle: string;
  prompt: string;
  tone: Tone;
};

type AccountSetting = {
  title: string;
  detail: string;
  action: string;
};

type GatewayRecord = Record<string, any>;

const QUICK_ACTIONS: QuickAction[] = [
  { key: 'code', icon: '</>', title: '代码协作', subtitle: '编写、调试与审查\n代码更高效', prompt: '帮我检查当前代码分支，拆分高风险变更，并生成可审查的 PR 计划。', tone: 'blue' },
  { key: 'data', icon: '▰', title: '数据采集', subtitle: '连接数据源，采集\n并清洗数据', prompt: '帮我生成今天的数据采集日报，并列出异常数据、缺失字段和后续动作。', tone: 'green' },
  { key: 'creator', icon: '●', title: '达人运营', subtitle: '洞察达人数据，优\n化投放策略', prompt: '分析达人投放数据，按照 BAD、NORMAL、AI good、AI bad、GOOD 归类并给出建议。', tone: 'violet' },
  { key: 'content', icon: '✎', title: '内容生成', subtitle: '生成文案、脚本与\n多媒体内容', prompt: '为当前产品生成 TikTok 内容脚本，口播要自然，不要太硬带货。', tone: 'orange' },
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

function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`status-pill tone-${tone}`}>{children}</span>;
}

function initialsFromProject(name: string) {
  const trimmed = name.trim();
  return (trimmed || 'P').slice(0, 2).toUpperCase();
}

function initialsFromActor(value: string) {
  const compact = value.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]+/g, '').trim();
  return (compact || 'WA').slice(0, 2).toUpperCase();
}

function projectFromGateway(project: GatewayRecord): ProjectItem {
  return {
    id: String(project.project_id || project.id || ''),
    name: String(project.name || 'Untitled project'),
    description: String(project.description || ''),
    ownerActor: String(project.owner_actor || ''),
    organizationId: String(project.organization_id || ''),
    metadata: project.metadata && typeof project.metadata === 'object' ? project.metadata : {},
    archived: Boolean(project.archived),
    createdAt: String(project.created_at || project.createdAt || 'n/a'),
    updatedAt: String(project.updated_at || project.updatedAt || project.created_at || project.createdAt || 'n/a'),
  };
}

function asRecordArray(value: unknown): GatewayRecord[] {
  return Array.isArray(value) ? value.filter((item): item is GatewayRecord => !!item && typeof item === 'object') : [];
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
      body: JSON.stringify({ gatewayUrl: baseUrl, token, actor, role }),
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

  function operationKey(prefix: string) {
    return `${prefix}-${crypto.randomUUID()}`;
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

  async function refreshProjects(activeApi: OmniAdminApi) {
    const result = await activeApi.projects();
    const loaded = asRecordArray(result.projects).map(projectFromGateway).filter((project) => project.id.length > 0);
    setProjects(loaded);
    setActiveProjectId((current) => current && loaded.some((project) => project.id === current) ? current : (loaded[0]?.id || ''));
    return loaded;
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
      await refreshProjects(activeApi);
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function createProject(projectName = newProjectName) {
    const name = projectName.trim();
    if (!name) {
      setProjectError('请输入项目名称。');
      return;
    }
    if (projects.some((project) => project.name.toLowerCase() === name.toLowerCase())) {
      setProjectError('项目已存在。');
      return;
    }
    setProjectError('');
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      const result = await activeApi.createProject(name, '', {}, { source: 'web_admin' }, operationKey('web-admin-project-create'));
      const project = projectFromGateway(result.project || {});
      setProjects((current) => [project, ...current.filter((item) => item.id !== project.id)]);
      setActiveProjectId(project.id);
      setNewProjectName('');
      setSnapshot(await activeApi.bootstrap());
    } catch (e: any) {
      setProjectError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function deleteProject(projectId: string) {
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      await activeApi.deleteProject(projectId, operationKey('web-admin-project-delete'));
      await refreshProjects(activeApi);
    } catch (e: any) {
      setProjectError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function updateActiveProject(action: 'rename' | 'archive') {
    const project = projects.find((item) => item.id === activeProjectId);
    if (!project) return;
    setLoading(true);
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      const name = action === 'rename' ? window.prompt('新项目名称', project.name)?.trim() : undefined;
      if (action === 'rename' && !name) return;
      await activeApi.updateProject(project.id, action === 'rename' ? { name } : { archived: !project.archived }, operationKey(`web-admin-project-${action}`));
      await refreshProjects(activeApi);
    } catch (e: any) {
      setProjectError(e.message || String(e));
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
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
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

  const activeProject = projects.find((project) => project.id === activeProjectId) || null;
  const activeProjectName = activeProject?.name || '未选择项目';
  const approvals = asRecordArray(snapshot?.pending_approvals);
  const notifications = asRecordArray(snapshot?.notifications);
  const devices = asRecordArray(snapshot?.devices);
  const runtime = (runtimeStatus?.runtime || snapshot?.runtime_status || {}) as GatewayRecord;
  const actorInitials = initialsFromActor(actor);
  const releaseEvidence = runtime.release_evidence || runtime.ga_evidence || runtimeStatus?.release_evidence || runtimeStatus?.ga_evidence || snapshot?.release_evidence || snapshot?.ga_evidence || null;
  const releaseEvidenceText = releaseEvidence ? (JSON.stringify(releaseEvidence, null, 2) || String(releaseEvidence)) : '尚未加载 release_evidence / GA evidence。';

  function pickPrompt(prompt: string) {
    setChatInput(activeProject ? `[${activeProject.name}] ${prompt}` : prompt);
  }

  return <main className="app-shell">
    <aside className="sidebar left-sidebar" aria-label="Primary navigation">
      <div className="window-row"><span className="traffic red" /><span className="traffic yellow" /><span className="traffic green" /></div>
      <nav className="primary-nav">
        <button className="nav-item nav-primary" type="button" onClick={() => setChatInput('')}>新对话 <span>＋</span></button>
        <button className="nav-item" type="button" disabled title="未启用">⌕ <span>搜索 · 未启用</span></button>
        <button className="nav-item" type="button" disabled title="未启用">◴ <span>已安排 · 未启用</span></button>
        <button className="nav-item" type="button" disabled title="未启用">✣ <span>插件 · 未启用</span></button>
      </nav>

      <section className="project-box">
        <div className="section-title"><span>项目</span><button type="button" onClick={() => void createProject(newProjectName || '新项目')} disabled={loading}>＋ 新建项目</button></div>
        <form className="project-create-form" onSubmit={(event) => { event.preventDefault(); void createProject(); }}>
          <input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} placeholder="输入项目名称后创建" />
          <button type="submit" disabled={loading}>创建</button>
        </form>
        {projectError && <p className="project-error">{projectError}</p>}
        <div className="project-list">
          {projects.length === 0 ? <div className="project-empty-state"><strong>暂无项目</strong><span>项目由 Gateway 创建，并跨 Web Admin / Desktop / Mobile 同步。</span></div> : projects.map((project) => <button
            className={`project-row ${project.id === activeProjectId ? 'active' : ''}`}
            key={project.id}
            type="button"
            onClick={() => setActiveProjectId(project.id)}
          >
            <span className="project-icon">{initialsFromProject(project.name)}</span>
            <span>{project.name}</span>
            <span className="row-more">{project.id === activeProjectId ? '...' : '☆'}</span>
          </button>)}
        </div>
      </section>

      <button className="profile-card profile-button" type="button" onClick={() => setAccountSettingsOpen((open) => !open)}>
        <div className="avatar">{actorInitials}</div>
        <div><strong>{actor}</strong><small>{role} · Web Admin</small></div>
        <span>{accountSettingsOpen ? '⌃' : '⌄'}</span>
      </button>
    </aside>

    <section className="workspace">
      <header className="topbar">
        <button className="workspace-switcher" type="button" disabled title="项目切换器未启用">▣ <span>{activeProjectName}</span>⌄</button>
        <label className="global-search" htmlFor="global-search"><span>⌕</span><input id="global-search" placeholder="全局搜索未启用" disabled /></label>
        <button className="connect-button" type="button" onClick={load} disabled={loading}>▦ 连接应用</button>
        <button className="top-avatar" type="button" aria-label="打开账户设置" onClick={() => setAccountSettingsOpen((open) => !open)}>{actorInitials}⌄</button>
      </header>

      <div className="hero-panel">
        <div className="hero-copy"><h1>我们应该在 AI 助理中做些什么？</h1><p>✦ 智能协作 · 深度思考 · Gateway 项目同步</p></div>
        <section className="composer-card" aria-label="AI assistant composer">
          <textarea value={chatInput} onChange={(event) => setChatInput(event.target.value)} placeholder={activeProject ? `在 ${activeProject.name} 中输入任务...` : '先创建项目，或直接向 AI 助理提问...'} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void askAssistant(); }} />
          <div className="composer-actions"><select value={chatProfile} onChange={(event) => setChatProfile(event.target.value)} aria-label="选择模型配置"><option value="fast">快速</option><option value="planner">规划</option><option value="local">本地</option></select><button className="send-button" type="button" onClick={askAssistant} disabled={!canAsk || loading}>↑</button></div>
        </section>
        {error && <div className="error-banner">{error}</div>}
      </div>

      <section className="side-card centered-card centered-card-first">
        <div className="card-title"><h3>Gateway 连接配置</h3><StatusPill tone={csrfToken ? 'green' : 'gray'}>{csrfToken ? 'session ready' : '未建立 session'}</StatusPill></div>
        <details className="connection-details" open>
          <summary>Gateway URL / Token / Actor / Role</summary>
          <label>Gateway URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
          <label>Session Token<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
          <label>Actor<input value={actor} onChange={(event) => setActor(event.target.value)} /></label>
          <label>Role<select value={role} onChange={(event) => setRole(event.target.value as AdminRole)}><option value="viewer">viewer</option><option value="operator">operator</option><option value="owner">owner</option></select></label>
          <p className="hint-text">{ROLE_HELP[role]}</p>
          <button className="secondary-action" type="button" onClick={() => { void establishSession(); }}>建立 Web Session / CSRF</button>
        </details>
      </section>

      <section className="quick-grid">
        {QUICK_ACTIONS.map((action) => <button key={action.key} className={`quick-card tone-card-${action.tone}`} type="button" onClick={() => pickPrompt(action.prompt)}><span>{action.icon}</span><strong>{action.title}</strong><small>{action.subtitle}</small></button>)}
      </section>

      <section className="metrics-grid">
        <div className="side-card"><div className="card-title"><h3>当前项目</h3><StatusPill tone={activeProject ? 'green' : 'gray'}>{activeProject ? (activeProject.archived ? 'archived' : 'ready') : '待创建'}</StatusPill></div><p>{activeProject ? `${activeProject.name} · ${formatDate(activeProject.updatedAt)}` : '请先连接 Gateway 并创建或选择项目。'}</p>{activeProject && <><button type="button" className="secondary-action" onClick={() => void updateActiveProject('rename')} disabled={loading}>重命名</button>{' '}<button type="button" className="secondary-action" onClick={() => void updateActiveProject('archive')} disabled={loading}>{activeProject.archived ? '恢复' : '归档'}</button>{' '}<button type="button" className="secondary-action" onClick={() => void deleteProject(activeProject.id)} disabled={loading}>删除</button></>}</div>
        <div className="side-card"><div className="card-title"><h3>设备</h3><StatusPill tone={deviceIdentity ? 'green' : 'gray'}>{deviceIdentity ? 'enrolled' : 'not enrolled'}</StatusPill></div><p>{deviceIdentity?.deviceId || '尚未注册 Web Admin 设备'}</p><p>Gateway devices: {devices.length}</p></div>
        <div className="side-card"><div className="card-title"><h3>审批</h3><StatusPill tone={approvals.length ? 'orange' : 'green'}>{approvals.length} pending</StatusPill></div>{approvals.slice(0, 3).map((approval) => <div className="mini-row" key={String(approval.approval_id)}><strong>{approval.action || 'Approval'}</strong><span>{approval.risk || 'risk n/a'}</span><button type="button" disabled={!canApprove || loading} onClick={() => void decide(String(approval.approval_id), 'approved')}>批准</button></div>)}</div>
        <div className="side-card"><div className="card-title"><h3>通知</h3><StatusPill tone={notifications.length ? 'blue' : 'gray'}>{notifications.length}</StatusPill></div>{notifications.slice(0, 4).map((item) => <p key={String(item.notification_id || item.title)}>{item.title || item.body}</p>)}</div>
      </section>

      <section className="side-card centered-card"><div className="card-title"><h3>最近对话</h3><StatusPill tone={chatMessages.length ? 'blue' : 'gray'}>{chatMessages.length}</StatusPill></div><pre>{JSON.stringify(chatMessages.slice(-6).map((message) => ({ role: message.role, content: message.content, provider: message.model_provider, model: message.model_name, trace_id: message.trace_id })), null, 2)}</pre></section>
      <section className="side-card centered-card"><div className="card-title"><h3>GA / Release Evidence</h3><StatusPill tone={releaseEvidence ? 'blue' : 'orange'}>{releaseEvidence ? 'loaded' : 'missing'}</StatusPill></div><pre>{releaseEvidenceText}</pre></section>
    </section>

    <aside className="right-rail">
      {accountSettingsOpen && <section className="side-card"><div className="card-title"><h3>账户设置</h3><StatusPill tone="blue">Codex-style</StatusPill></div>{ACCOUNT_SETTINGS.map((setting) => <button className="setting-row" key={setting.title} type="button"><span><strong>{setting.title}</strong><small>{setting.detail}</small></span><em>{setting.action}</em></button>)}</section>}
      <section className="side-card"><div className="card-title"><h3>Gateway 状态</h3><StatusPill tone={loading ? 'orange' : 'green'}>{loading ? 'loading' : 'idle'}</StatusPill></div><p>Role: {role}</p><p>Actor: {actor}</p><p>Projects: {projects.length}</p></section>
      <section className="side-card"><div className="card-title"><h3>集成状态</h3><StatusPill tone={ecosystem ? 'blue' : 'gray'}>{ecosystem ? 'loaded' : 'not loaded'}</StatusPill></div><pre>{JSON.stringify(ecosystem?.channels || [], null, 2)}</pre></section>
    </aside>
  </main>;
}
