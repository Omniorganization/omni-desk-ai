import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { OmniApiClient } from './api';
import { buildControlHubPanels } from './controlHub';
import { executeRuntimeTask } from './executor';
import { createDesktopDeviceRequestSigner, loadOrCreateDesktopIdentity, DesktopDeviceIdentity } from './deviceIdentity';

const DEFAULT_GATEWAY = 'http://127.0.0.1:18789';
const VERSION = '1.12.7';
const CAPABILITIES = ['chat', 'local-runtime', 'browser', 'files', 'ui-bridge', 'sandbox'];

type ProjectItem = {
  id: string;
  name: string;
  description: string;
  ownerActor: string;
  organizationId: string;
  metadata: Record<string, unknown>;
  archived: boolean;
  createdAt: string;
  updatedAt: string;
};

type GatewayProject = {
  project_id?: unknown;
  id?: unknown;
  name?: unknown;
  description?: unknown;
  owner_actor?: unknown;
  organization_id?: unknown;
  metadata?: unknown;
  archived?: unknown;
  created_at?: unknown;
  createdAt?: unknown;
  updated_at?: unknown;
  updatedAt?: unknown;
};

type QuickAction = {
  icon: string;
  title: string;
  detail: string;
  prompt: string;
};

type AccountSetting = {
  title: string;
  detail: string;
  action: string;
};

const QUICK_ACTIONS: QuickAction[] = [
  {
    icon: '</>',
    title: '代码协作',
    detail: '本地运行、终端、diff 与安全执行',
    prompt: '在本地运行器中检查当前任务，生成可执行计划并列出需要审批的高风险动作。',
  },
  {
    icon: '▣',
    title: '任务执行',
    detail: 'Claim task、worker cycle、状态回写',
    prompt: '领取一个桌面任务，评估是否可安全执行，并说明执行前需要哪些证据。',
  },
  {
    icon: '▰',
    title: '证据采集',
    detail: '真实构建、签名、回滚与日志证据',
    prompt: '为当前项目整理桌面端真实运行证据，包括构建、签名、回滚和运行日志。',
  },
  {
    icon: '⚙',
    title: '环境治理',
    detail: 'Keychain、设备身份、沙盒策略',
    prompt: '检查本地 Desktop Runtime 的设备身份、Keychain 保存、沙盒执行边界和风险点。',
  },
];

const ACCOUNT_SETTINGS: AccountSetting[] = [
  { title: '账户资料', detail: '头像、名称、邮箱与本机操作者身份', action: '管理' },
  { title: '工作区与组织', detail: '团队、成员、权限与本机设备归属', action: '打开' },
  { title: '自定义指令', detail: '默认语气、项目偏好与执行约束', action: '编辑' },
  { title: 'Skills / 工作流', detail: '本地任务模板、运行手册与自动化技能', action: '配置' },
  { title: '连接器', detail: 'GitHub、AWS、Drive、Slack 等连接状态', action: '连接' },
  { title: 'GitHub 仓库', detail: '仓库、PR、分支、review 与本地 worktree', action: '同步' },
  { title: '执行环境', detail: 'Local、Cloud、worktree、终端与 sandbox', action: '设置' },
  { title: 'Secrets / 环境变量', detail: 'Keychain、token、密钥与环境变量', action: '管理' },
  { title: '通知', detail: '审批、任务完成、失败与评论提醒', action: '设置' },
  { title: '外观', detail: '主题、密度、语言、快捷键与侧栏显示', action: '调整' },
  { title: '数据控制', detail: '记忆、历史记录、导出、删除与隐私边界', action: '查看' },
  { title: '安全与登录', detail: '设备、会话、签名请求与退出登录', action: '检查' },
];

async function keychainGet(key: string): Promise<string> {
  try { return await invoke<string>('secure_get', { key }); } catch { return ''; }
}

async function keychainSet(key: string, value: string): Promise<void> {
  await invoke('secure_set', { key, value });
}

function projectInitials(name: string) {
  return name.trim().slice(0, 2).toUpperCase() || 'P';
}

function actorInitials(value: string) {
  return value.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]+/g, '').trim().slice(0, 2).toLowerCase() || 'op';
}

function operationKey(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function projectFromGateway(project: GatewayProject): ProjectItem {
  return {
    id: String(project.project_id || project.id || ''),
    name: String(project.name || 'Untitled project'),
    description: String(project.description || ''),
    ownerActor: String(project.owner_actor || ''),
    organizationId: String(project.organization_id || ''),
    metadata: project.metadata && typeof project.metadata === 'object' ? project.metadata as Record<string, unknown> : {},
    archived: Boolean(project.archived),
    createdAt: String(project.created_at || project.createdAt || new Date().toISOString()),
    updatedAt: String(project.updated_at || project.updatedAt || project.created_at || project.createdAt || new Date().toISOString()),
  };
}

function App() {
  const [gatewayUrl, setGatewayUrl] = useState(DEFAULT_GATEWAY);
  const [token, setToken] = useState('');
  const [actor, setActor] = useState('desktop-operator');
  const [snapshot, setSnapshot] = useState<any>(null);
  const [claimedTask, setClaimedTask] = useState<any>(null);
  const [autoWorkerEnabled, setAutoWorkerEnabled] = useState(false);
  const [workerLog, setWorkerLog] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [deviceIdentity, setDeviceIdentity] = useState<DesktopDeviceIdentity | null>(null);
  const [chatConversationId, setChatConversationId] = useState('');
  const [chatInput, setChatInput] = useState('Summarize current OmniDesk runtime state.');
  const [chatProfile, setChatProfile] = useState('fast');
  const [chatMessages, setChatMessages] = useState<any[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [activeProjectId, setActiveProjectId] = useState('');
  const [newProjectName, setNewProjectName] = useState('');
  const [projectError, setProjectError] = useState('');
  const [showAccountSettings, setShowAccountSettings] = useState(true);
  const controlHubPanels = useMemo(() => buildControlHubPanels(snapshot, null), [snapshot]);
  const client = useMemo(() => new OmniApiClient({
    baseUrl: gatewayUrl.replace(/\/$/, ''),
    token,
    actor,
    deviceSigner: deviceIdentity ? createDesktopDeviceRequestSigner(deviceIdentity.deviceId) : undefined,
  }), [gatewayUrl, token, actor, deviceIdentity]);

  useEffect(() => {
    keychainGet('omni.gateway').then(v => v && setGatewayUrl(v));
    keychainGet('omni.operatorToken').then(v => v && setToken(v));
    keychainGet('omni.actor').then(v => v && setActor(v));
  }, []);

  async function refreshProjects(activeClient = client): Promise<ProjectItem[]> {
    const result = await activeClient.projects();
    const rawProjects: unknown[] = Array.isArray(result.projects) ? result.projects : [];
    const loaded: ProjectItem[] = rawProjects
      .map((project: unknown) => projectFromGateway(project && typeof project === 'object' ? project as GatewayProject : {}))
      .filter((project: ProjectItem) => project.id);
    setProjects(loaded);
    setActiveProjectId(current => current && loaded.some(project => project.id === current) ? current : (loaded[0]?.id || ''));
    return loaded;
  }

  async function connect() {
    setError('');
    try {
      await keychainSet('omni.gateway', gatewayUrl);
      await keychainSet('omni.operatorToken', token);
      await keychainSet('omni.actor', actor);
      const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
      setDeviceIdentity(identity);
      const signedClient = new OmniApiClient({
        baseUrl: gatewayUrl.replace(/\/$/, ''),
        token,
        actor,
        deviceSigner: createDesktopDeviceRequestSigner(identity.deviceId),
      });
      await signedClient.registerDesktop(identity.deviceId, navigator.platform, CAPABILITIES, identity.publicKeyPem);
      await signedClient.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
      setSnapshot(await signedClient.bootstrap());
      await refreshProjects(signedClient);
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  async function claim() {
    setError('');
    try {
      const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
      setDeviceIdentity(identity);
      const result = await client.claimTask(identity.deviceId, CAPABILITIES, 60);
      setClaimedTask(result.task || null);
      if (result.task) {
        await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, result.task.task_id);
      }
      setSnapshot(await client.bootstrap());
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  async function complete(status: 'completed' | 'failed') {
    if (!claimedTask?.task_id) return;
    await client.updateTaskStatus(claimedTask.task_id, status, status === 'completed' ? 'Completed by Omni Desktop Runtime' : 'Failed by Omni Desktop Runtime', deviceIdentity?.deviceId);
    setWorkerLog(items => [`${new Date().toISOString()} ${status}: ${claimedTask.task_id}`, ...items].slice(0, 20));
    setClaimedTask(null);
    setSnapshot(await client.bootstrap());
  }

  async function runSafeWorkerCycle() {
    const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
    setDeviceIdentity(identity);
    const result = await client.claimTask(identity.deviceId, CAPABILITIES, 120);
    const task = result.task || null;
    if (!task) {
      await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES);
      return;
    }
    setClaimedTask(task);
    await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, task.task_id);
    const execution = await executeRuntimeTask(task);
    await client.updateTaskStatus(task.task_id, execution.status, execution.summary, identity.deviceId);
    setWorkerLog(items => [`${new Date().toISOString()} ${execution.status}: ${execution.summary}`, ...items].slice(0, 20));
    setClaimedTask(null);
    setSnapshot(await client.bootstrap());
  }

  async function askAssistant() {
    setError('');
    try {
      const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
      setDeviceIdentity(identity);
      let conversationId = chatConversationId;
      if (!conversationId) {
        const created = await client.createConversation('Desktop Ask Mode', identity.deviceId);
        conversationId = created.conversation.conversation_id;
        setChatConversationId(conversationId);
      }
      const result = await client.askConversation(conversationId, chatInput, chatProfile, identity.deviceId);
      const messages = await client.listMessages(conversationId);
      setChatMessages(messages.messages || [result.user_message, result.assistant_message]);
      setSnapshot(await client.bootstrap());
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  async function createProject(projectName = newProjectName) {
    const name = projectName.trim();
    if (!name) {
      setProjectError('请输入项目名称。');
      return;
    }
    if (projects.some(project => project.name.toLowerCase() === name.toLowerCase())) {
      setProjectError('项目已存在。');
      return;
    }
    setProjectError('');
    try {
      const result = await client.createProject(name, '', {}, deviceIdentity?.deviceId, operationKey('desktop-project-create'));
      const project = projectFromGateway(result.project);
      setProjects(current => [project, ...current.filter(item => item.id !== project.id)]);
      setActiveProjectId(project.id);
      setNewProjectName('');
      setSnapshot(await client.bootstrap());
    } catch (e: any) {
      setProjectError(e.message || String(e));
    }
  }

  async function mutateProject(action: 'rename' | 'archive' | 'delete') {
    const project = projects.find(item => item.id === activeProjectId);
    if (!project) return;
    setProjectError('');
    try {
      if (action === 'delete') {
        await client.deleteProject(project.id, operationKey('desktop-project-delete'));
      } else {
        const payload = action === 'archive'
          ? { archived: !project.archived }
          : { name: window.prompt('新项目名称', project.name)?.trim() };
        if (action === 'rename' && !payload.name) return;
        await client.updateProject(project.id, payload, operationKey(`desktop-project-${action}`));
      }
      await refreshProjects();
    } catch (e: any) {
      setProjectError(e.message || String(e));
    }
  }

  useEffect(() => {
    if (!token) return;
    const timer = window.setInterval(async () => {
      try {
        if (autoWorkerEnabled) {
          await runSafeWorkerCycle();
        } else {
          const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
          setDeviceIdentity(identity);
          await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
          setSnapshot(await client.bootstrap());
        }
      } catch (e: any) {
        setError(e.message || String(e));
      }
    }, autoWorkerEnabled ? 5000 : 15000);
    return () => window.clearInterval(timer);
  }, [client, token, claimedTask, autoWorkerEnabled]);

  const activeProject = projects.find(project => project.id === activeProjectId) || null;
  const activeProjectName = activeProject?.name || '未选择项目';
  const pendingApprovals = snapshot?.pending_approvals || [];
  const notifications = snapshot?.notifications || [];

  function usePrompt(prompt: string) {
    setChatInput(activeProject ? `[${activeProject.name}] ${prompt}` : prompt);
  }

  return <main className="desktop-shell">
    <aside className="desktop-sidebar">
      <div className="traffic-row"><span className="dot red" /><span className="dot yellow" /><span className="dot green" /><strong>AI 助理 Desktop</strong></div>
      <nav className="desktop-nav">
        <button type="button" onClick={() => setChatInput('')}>＋ 新对话</button>
        <button type="button">⌕ 搜索</button>
        <button type="button">◴ 已安排</button>
        <button type="button">✣ 插件</button>
      </nav>

      <section className="panel projects-panel">
        <div className="panel-title"><span>项目</span><button type="button" onClick={() => void createProject(newProjectName || '新项目')}>＋ 新建项目</button></div>
        <form className="project-form" onSubmit={event => { event.preventDefault(); void createProject(); }}>
          <input value={newProjectName} onChange={event => setNewProjectName(event.target.value)} placeholder="输入项目名称后创建" />
          <button type="submit">创建</button>
        </form>
        {projectError && <p className="error small-error">{projectError}</p>}
        <div className="project-list">
          {projects.length === 0 ? <div className="empty-state"><strong>暂无项目</strong><span>Desktop 项目由 Gateway 创建并跨 Web / Mobile 同步；连接后会自动加载组织项目。</span></div> : projects.map(project => <button
            className={`project-row ${project.id === activeProjectId ? 'active' : ''}`}
            key={project.id}
            type="button"
            onClick={() => setActiveProjectId(project.id)}
          ><span>{projectInitials(project.name)}</span><strong>{project.name}</strong><em>...</em></button>)}
        </div>
      </section>

      <button className="profile-row" type="button" onClick={() => setShowAccountSettings(open => !open)}><span className="avatar">{actorInitials(actor)}</span><span><strong>{actor}</strong><small>Desktop operator</small></span><em>{showAccountSettings ? '⌃' : '⌄'}</em></button>
    </aside>

    <section className="desktop-workspace">
      <header className="topbar"><button type="button" className="workspace-switcher">▣ {activeProjectName} ⌄</button><button type="button" onClick={connect}>▦ 连接 Gateway</button><button type="button" onClick={claim}>Claim Task</button><label className="worker-toggle"><input type="checkbox" checked={autoWorkerEnabled} onChange={e => setAutoWorkerEnabled(e.target.checked)} /> 自动 Worker</label></header>

      <section className="hero">
        <div className="orb" />
        <h1>Desktop Control Hub</h1>
        <p>本地 Agent 运行器：项目、账户设置、终端任务、审批证据与模型问答保持同一套 Codex-style 工作流。</p>
        <section className="composer">
          <textarea value={chatInput} onChange={event => setChatInput(event.target.value)} placeholder={activeProject ? `在 ${activeProject.name} 中输入本地执行任务...` : '先创建项目，或直接向 AI 助理提问...'} />
          <div className="composer-actions"><button type="button">＋ 附件</button><select value={chatProfile} onChange={event => setChatProfile(event.target.value)}><option value="fast">快速</option><option value="planner">规划</option><option value="local">本地</option></select><button type="button" className="send" onClick={askAssistant}>↑</button></div>
        </section>
        {error && <p className="error">{error}</p>}
      </section>

      <section className="quick-grid">
        {QUICK_ACTIONS.map(action => <button className="quick-card" type="button" key={action.title} onClick={() => usePrompt(action.prompt)}><span>{action.icon}</span><strong>{action.title}</strong><small>{action.detail}</small><em>→</em></button>)}
      </section>

      <section className="grid compact">
        {controlHubPanels.map(panel => <div className={`panel hub ${panel.status}`} key={panel.id}><h2>{panel.title}</h2><strong>{panel.status}</strong><p>{panel.count}</p></div>)}
      </section>

      <section className="grid data-grid">
        <div className="panel"><h2>Claimed Task</h2><pre>{JSON.stringify(claimedTask || {}, null, 2)}</pre><button disabled={!claimedTask} onClick={() => complete('completed')}>标记完成</button>{' '}<button disabled={!claimedTask} onClick={() => complete('failed')}>标记失败</button><h3>Worker log</h3><pre>{workerLog.join('\n')}</pre></div>
        <div className="panel"><h2>最近对话</h2><pre>{JSON.stringify(chatMessages.slice(-6).map(message => ({ role: message.role, content: message.content, provider: message.model_provider, model: message.model_name, profile: message.model_profile, audit_trace_id: message.trace_id })), null, 2)}</pre></div>
        <div className="panel"><h2>Runtime</h2><pre>{JSON.stringify(snapshot?.runtime_status || [], null, 2)}</pre></div>
      </section>
    </section>

    <aside className="desktop-rightbar">
      {showAccountSettings && <section className="panel account-panel"><div className="panel-title"><h2>账户设置</h2><span>Codex-style</span></div>{ACCOUNT_SETTINGS.map(setting => <button className="setting-row" key={setting.title} type="button"><span><strong>{setting.title}</strong><small>{setting.detail}</small></span><em>{setting.action}</em></button>)}</section>}
      <section className="panel"><div className="panel-title"><h2>当前项目</h2><span>{activeProject ? (activeProject.archived ? 'archived' : 'ready') : '待创建'}</span></div><p>{activeProject ? `${activeProject.name} 已从 Gateway 同步到 Desktop Runtime。` : '请先连接 Gateway 并创建或选择项目。'}</p>{activeProject && <p><button type="button" onClick={() => void mutateProject('rename')}>重命名</button>{' '}<button type="button" onClick={() => void mutateProject('archive')}>{activeProject.archived ? '恢复' : '归档'}</button>{' '}<button type="button" onClick={() => void mutateProject('delete')}>删除</button></p>}<p>Device: {deviceIdentity?.deviceId || 'not enrolled'}</p></section>
      <section className="panel"><h2>连接配置</h2><label>Gateway URL<input value={gatewayUrl} onChange={e => setGatewayUrl(e.target.value)} /></label><label>Operator Token<input type="password" value={token} onChange={e => setToken(e.target.value)} autoComplete="off" /></label><label>Actor<input value={actor} onChange={e => setActor(e.target.value)} /></label></section>
      <section className="panel"><h2>审批状态</h2><p>待审批：{pendingApprovals.length}</p><pre>{JSON.stringify(pendingApprovals.slice(0, 3), null, 2)}</pre></section>
      <section className="panel"><h2>通知</h2><p>{notifications.length} 条</p><pre>{JSON.stringify(notifications.slice(0, 5), null, 2)}</pre></section>
    </aside>

    <style>{`
      body { margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif; background: radial-gradient(circle at 45% 10%, rgba(82,105,255,.24), transparent 24rem), #08101a; color: #f6f8ff; overflow: hidden; }
      button, input, textarea, select { font: inherit; }
      button { cursor: pointer; color: inherit; }
      button:disabled { opacity: .45; cursor: not-allowed; }
      .desktop-shell { width: 100vw; height: 100vh; display: grid; grid-template-columns: 292px minmax(620px, 1fr) 336px; overflow: hidden; }
      .desktop-sidebar, .desktop-rightbar { background: rgba(7,14,25,.72); border-color: rgba(150,168,205,.16); overflow: auto; padding: 16px; }
      .desktop-sidebar { border-right: 1px solid rgba(150,168,205,.16); display: flex; flex-direction: column; gap: 14px; }
      .desktop-rightbar { border-left: 1px solid rgba(150,168,205,.16); }
      .desktop-workspace { overflow: auto; }
      .traffic-row { display: flex; align-items: center; gap: 10px; height: 34px; color: #d9e3f5; }
      .dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
      .red { background: #ff5f57; } .yellow { background: #ffbd2e; } .green { background: #28c840; }
      .desktop-nav { display: grid; gap: 7px; }
      .desktop-nav button, .workspace-switcher, .topbar button, .worker-toggle { min-height: 40px; border: 1px solid rgba(150,168,205,.16); background: rgba(255,255,255,.05); border-radius: 13px; padding: 0 13px; text-align: left; }
      .desktop-nav button:first-child { background: linear-gradient(135deg, rgba(91,105,255,.68), rgba(45,65,150,.5)); border-color: rgba(145,160,255,.55); color: white; }
      .panel, .profile-row, .composer, .quick-card { border: 1px solid rgba(150,168,205,.16); background: linear-gradient(145deg, rgba(23,34,53,.82), rgba(10,19,32,.76)); border-radius: 18px; padding: 16px; box-shadow: inset 0 1px rgba(255,255,255,.05); }
      .panel-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; color: #a8b5ca; font-size: 13px; }
      .panel-title h2 { color: #fff; margin: 0; font-size: 15px; }
      .panel-title button, .project-form button { border: 1px solid rgba(128,150,255,.42); background: rgba(101,135,255,.16); color: #dfe6ff; border-radius: 11px; padding: 7px 10px; }
      .projects-panel { flex: 1; min-height: 0; }
      .project-form { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-bottom: 10px; }
      input, textarea, select { width: 100%; margin-top: 6px; padding: 10px; border-radius: 11px; border: 1px solid rgba(150,168,205,.18); background: rgba(255,255,255,.045); color: white; outline: none; }
      textarea { min-height: 96px; resize: vertical; }
      label { display: block; margin: 10px 0; color: #aebbd0; }
      .project-list { display: grid; gap: 6px; max-height: calc(100vh - 330px); overflow: auto; }
      .project-row { display: grid; grid-template-columns: 28px 1fr auto; gap: 8px; align-items: center; border: 1px solid transparent; background: transparent; color: #d7e0f2; border-radius: 13px; padding: 9px; text-align: left; }
      .project-row span { width: 26px; height: 26px; display: grid; place-items: center; border-radius: 8px; background: rgba(255,255,255,.07); font-size: 10px; }
      .project-row.active { background: linear-gradient(135deg, rgba(75,89,210,.75), rgba(38,58,141,.62)); border-color: rgba(145,160,255,.45); }
      .empty-state { border: 1px dashed rgba(160,178,215,.28); border-radius: 14px; padding: 16px; display: grid; gap: 8px; color: #dbe5f7; line-height: 1.5; }
      .empty-state span, small, .setting-row small { color: #93a1b5; }
      .profile-row { display: grid; grid-template-columns: 42px 1fr 18px; align-items: center; gap: 10px; text-align: left; }
      .avatar { width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center; background: linear-gradient(135deg, #5dcaff, #6674ff); }
      .topbar { height: 70px; display: flex; align-items: center; gap: 12px; padding: 0 22px; border-bottom: 1px solid rgba(150,168,205,.16); background: rgba(8,14,25,.66); position: sticky; top: 0; z-index: 5; }
      .worker-toggle { display: inline-flex; align-items: center; gap: 8px; }
      .worker-toggle input { width: auto; margin: 0; }
      .hero { max-width: 920px; margin: 48px auto 24px; text-align: center; position: relative; }
      .hero h1 { font-size: 48px; letter-spacing: -.045em; margin: 0 0 12px; }
      .hero p { color: #9ca9bd; }
      .orb { position: absolute; width: 400px; height: 180px; left: 50%; top: -28px; transform: translateX(-50%) rotate(-6deg); border-radius: 50%; border: 1px solid rgba(111,130,255,.3); box-shadow: 0 0 80px rgba(91,105,255,.25); pointer-events: none; }
      .composer { text-align: left; border-color: rgba(128,150,255,.42); }
      .composer-actions { display: flex; justify-content: flex-end; gap: 10px; align-items: center; }
      .composer-actions button, .send { border: 1px solid rgba(150,168,205,.16); background: rgba(255,255,255,.06); border-radius: 12px; padding: 10px 12px; }
      .send { width: 44px; height: 44px; border-radius: 50%; background: linear-gradient(135deg, #7b8dff, #5269ff) !important; }
      .quick-grid, .grid { max-width: 920px; margin: 0 auto 18px; display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
      .quick-card { text-align: left; min-height: 140px; display: grid; gap: 8px; position: relative; }
      .quick-card span { font-size: 22px; color: #8fa5ff; }
      .quick-card em { position: absolute; right: 16px; bottom: 14px; width: 30px; height: 30px; border-radius: 50%; background: rgba(255,255,255,.1); display: grid; place-items: center; font-style: normal; }
      .compact { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
      .data-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
      .hub h2 { font-size: 14px; margin: 0 0 10px; }
      .hub strong { text-transform: uppercase; font-size: 12px; }
      .hub p { font-size: 24px; margin: 8px 0 0; }
      .hub.passed { border-color: #3b7f5f; } .hub.blocked { border-color: #925052; } .hub.pending { border-color: #88774c; }
      .setting-row { width: 100%; display: grid; grid-template-columns: 1fr auto; gap: 10px; text-align: left; align-items: center; border: 1px solid transparent; background: rgba(255,255,255,.035); border-radius: 13px; padding: 10px; color: #d9e3f4; margin-bottom: 8px; }
      .setting-row:hover { border-color: rgba(132,154,255,.35); background: rgba(118,143,255,.1); }
      .setting-row em { font-style: normal; border: 1px solid rgba(150,168,205,.16); border-radius: 999px; padding: 4px 8px; color: #c4cfe2; font-size: 12px; }
      pre { overflow: auto; max-height: 320px; color: #d8dee9; }
      .error { color: #ff9aad; } .small-error { margin: 0 0 8px; font-size: 12px; }
    `}</style>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
