import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { OmniApiClient, type ChatStreamEvent } from './api';
import { buildControlHubPanels } from './controlHub';
import { advertisedRuntimeCapabilities, executeRuntimeTask } from './executor';
import { createDesktopDeviceRequestSigner, loadOrCreateDesktopIdentity, DesktopDeviceIdentity } from './deviceIdentity';
import { flushStatusOutbox, runDurableWorkerCycle } from './runtimeWorker';

const DEFAULT_GATEWAY = 'http://127.0.0.1:18789';
const VERSION = '1.12.7';
const CAPABILITIES = advertisedRuntimeCapabilities();

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

type GatewayProject = Partial<Record<
  'project_id' | 'id' | 'name' | 'description' | 'owner_actor' | 'organization_id' |
  'metadata' | 'archived' | 'created_at' | 'createdAt' | 'updated_at' | 'updatedAt',
  unknown
>>;

const QUICK_ACTIONS = [
  ['</>', '代码协作', '本地运行、diff 与受审批文件操作', '检查当前任务，生成安全执行计划并列出需要审批的动作。'],
  ['▣', '任务执行', 'Lease、取消、恢复与状态回写', '领取桌面任务并说明执行前置条件、超时和恢复策略。'],
  ['▰', '证据采集', '构建、签名、回滚与运行日志', '整理当前项目所需的真实运行证据，不虚构未产生的外部证据。'],
  ['⚙', '环境治理', '设备身份、Keychain 与 Workspace 边界', '检查本地设备身份、沙盒、Workspace 和密钥边界。'],
] as const;

const ACCOUNT_SETTINGS = [
  ['账户资料', '头像、名称、邮箱与本机操作者身份'],
  ['工作区与组织', '团队、成员、权限与本机设备归属'],
  ['自定义指令', '默认语气、项目偏好与执行约束'],
  ['Skills / 工作流', '本地任务模板、运行手册与自动化技能'],
  ['连接器', 'GitHub、AWS、Drive、Slack 等连接状态'],
  ['执行环境', 'Local、Cloud、worktree 与 sandbox'],
  ['Secrets / 环境变量', 'Keychain、token、密钥与环境变量'],
  ['通知', '审批、任务完成、失败与评论提醒'],
  ['数据控制', '记忆、历史记录、导出与隐私边界'],
  ['安全与登录', '设备、会话与签名请求'],
] as const;

async function keychainGet(key: string): Promise<string> {
  try { return await invoke<string>('secure_get', { key }); } catch { return ''; }
}
async function keychainSet(key: string, value: string): Promise<void> { await invoke('secure_set', { key, value }); }
function operationKey(prefix: string) { return `${prefix}-${crypto.randomUUID()}`; }
function initials(value: string) { return value.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]+/g, '').slice(0, 2).toUpperCase() || 'OP'; }

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
  const workerBusy = useRef(false);
  const chatAbort = useRef<AbortController | null>(null);
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
  const [chatStreaming, setChatStreaming] = useState(false);
  const [chatStatus, setChatStatus] = useState('idle');
  const [chatLastEventId, setChatLastEventId] = useState(0);
  const [chatTraceId, setChatTraceId] = useState('');
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
    void keychainGet('omni.gateway').then(value => value && setGatewayUrl(value));
    void keychainGet('omni.operatorToken').then(value => value && setToken(value));
    void keychainGet('omni.actor').then(value => value && setActor(value));
  }, []);

  async function refreshProjects(activeClient = client): Promise<void> {
    const result = await activeClient.projects();
    const loaded = (Array.isArray(result.projects) ? result.projects : [])
      .map((project: unknown) => projectFromGateway(project && typeof project === 'object' ? project as GatewayProject : {}))
      .filter((project: ProjectItem) => project.id);
    setProjects(loaded);
    setActiveProjectId(current => current && loaded.some(project => project.id === current) ? current : loaded[0]?.id || '');
  }

  async function identity(): Promise<DesktopDeviceIdentity> {
    const loaded = deviceIdentity || await loadOrCreateDesktopIdentity();
    setDeviceIdentity(loaded);
    return loaded;
  }

  async function connect() {
    setError('');
    try {
      await Promise.all([
        keychainSet('omni.gateway', gatewayUrl),
        keychainSet('omni.operatorToken', token),
        keychainSet('omni.actor', actor),
      ]);
      const currentIdentity = await identity();
      const signedClient = new OmniApiClient({
        baseUrl: gatewayUrl.replace(/\/$/, ''),
        token,
        actor,
        deviceSigner: createDesktopDeviceRequestSigner(currentIdentity.deviceId),
      });
      await signedClient.registerDesktop(currentIdentity.deviceId, navigator.platform, CAPABILITIES, currentIdentity.publicKeyPem);
      await flushStatusOutbox(signedClient);
      await signedClient.heartbeat(currentIdentity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
      setSnapshot(await signedClient.bootstrap());
      await refreshProjects(signedClient);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function claim() {
    setError('');
    try {
      const currentIdentity = await identity();
      const result = await client.claimTask(currentIdentity.deviceId, CAPABILITIES, 120);
      setClaimedTask(result.task || null);
      if (result.task) await client.heartbeat(currentIdentity.deviceId, 'online', VERSION, CAPABILITIES, result.task.task_id);
      setSnapshot(await client.bootstrap());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function complete(status: 'completed' | 'failed') {
    if (!claimedTask?.task_id) return;
    try {
      await client.updateTaskStatus(
        claimedTask.task_id,
        status,
        status === 'completed' ? 'Completed by Omni Desktop Runtime' : 'Failed by Omni Desktop Runtime',
        deviceIdentity?.deviceId,
        operationKey(`desktop-manual-${status}`),
      );
      setWorkerLog(items => [`${new Date().toISOString()} ${status}: ${claimedTask.task_id}`, ...items].slice(0, 20));
      setClaimedTask(null);
      setSnapshot(await client.bootstrap());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function runSafeWorkerCycle() {
    if (workerBusy.current) return;
    workerBusy.current = true;
    try {
      const currentIdentity = await identity();
      await client.heartbeat(currentIdentity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
      await runDurableWorkerCycle(
        client,
        currentIdentity.deviceId,
        CAPABILITIES,
        (task, signal) => executeRuntimeTask(task, signal),
        {
          onClaimed: setClaimedTask,
          onLog: message => setWorkerLog(items => [message, ...items].slice(0, 20)),
          onSnapshot: setSnapshot,
        },
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      workerBusy.current = false;
    }
  }

  function applyStreamEvent(event: ChatStreamEvent, assistantId: string) {
    setChatLastEventId(event.id);
    if (event.event === 'chat.started') {
      const conversationId = String(event.data.conversation_id || '');
      if (conversationId) setChatConversationId(conversationId);
      setChatStatus('streaming');
    } else if (event.event === 'chat.delta') {
      const text = String(event.data.text || '');
      setChatMessages(current => current.map(message => message.message_id === assistantId ? { ...message, content: `${message.content || ''}${text}` } : message));
    } else if (event.event === 'chat.reasoning.delta') {
      const reasoning = String(event.data.text || '');
      setChatMessages(current => current.map(message => message.message_id === assistantId ? { ...message, reasoning: `${message.reasoning || ''}${reasoning}` } : message));
    } else if (event.event === 'chat.usage') {
      setChatStatus(`usage ${JSON.stringify(event.data)}`);
    } else if (event.event === 'chat.completed') {
      setChatTraceId(String(event.data.audit_trace_id || ''));
      setChatStatus(event.data.native === false ? 'completed · compatible fallback' : 'completed · native stream');
    } else if (event.event === 'chat.failed') {
      setChatStatus(`failed · ${String(event.data.code || 'chat_stream_failed')}`);
    }
  }

  async function askAssistant() {
    const prompt = chatInput.trim();
    if (!prompt || chatStreaming) return;
    setError('');
    setChatStreaming(true);
    setChatLastEventId(0);
    setChatStatus('starting');
    const controller = new AbortController();
    chatAbort.current = controller;
    const idempotencyKey = operationKey('desktop-stream');
    const assistantId = `stream-${crypto.randomUUID()}`;
    try {
      const currentIdentity = await identity();
      let conversationId = chatConversationId;
      if (!conversationId) {
        const created = await client.createConversation('Desktop Streaming Ask Mode', currentIdentity.deviceId);
        conversationId = String(created.conversation.conversation_id);
        setChatConversationId(conversationId);
      }
      setChatMessages(current => [
        ...current,
        { message_id: crypto.randomUUID(), role: 'user', content: prompt },
        { message_id: assistantId, role: 'assistant', content: '', reasoning: '' },
      ]);

      let cursor = 0;
      let observed = false;
      let reconnects = 0;
      while (true) {
        try {
          const result = await client.streamChat({
            conversationId,
            content: prompt,
            modelProfile: chatProfile,
            sourceDeviceId: currentIdentity.deviceId,
            idempotencyKey,
            lastEventId: cursor,
            signal: controller.signal,
            onEvent: event => {
              observed = true;
              cursor = event.id;
              applyStreamEvent(event, assistantId);
            },
          });
          cursor = result.lastEventId;
          break;
        } catch (cause) {
          if (controller.signal.aborted) throw cause;
          if (observed && reconnects < 1) {
            reconnects += 1;
            setChatStatus(`reconnecting from event ${cursor}`);
            continue;
          }
          if (observed) throw cause;
          setChatStatus('stream unavailable · non-streaming fallback');
          const fallback = await client.askConversation(
            conversationId,
            prompt,
            chatProfile,
            currentIdentity.deviceId,
            idempotencyKey,
          );
          const assistant = fallback.assistant_message || {};
          setChatMessages(current => current.map(message => message.message_id === assistantId ? { ...message, content: String(assistant.content || '') } : message));
          setChatTraceId(String(fallback.audit_trace_id || assistant.trace_id || ''));
          setChatStatus('completed · non-streaming fallback');
          break;
        }
      }
      setChatLastEventId(cursor);
      const persisted = await client.listMessages(conversationId);
      if (Array.isArray(persisted.messages)) setChatMessages(persisted.messages);
      setSnapshot(await client.bootstrap());
      setChatInput('');
    } catch (cause) {
      if (controller.signal.aborted) setChatStatus('cancelled by operator');
      else {
        setError(cause instanceof Error ? cause.message : String(cause));
        setChatStatus('failed');
      }
    } finally {
      chatAbort.current = null;
      setChatStreaming(false);
    }
  }

  function stopChat() {
    chatAbort.current?.abort('operator_stop');
  }

  function newConversation() {
    stopChat();
    setChatConversationId('');
    setChatMessages([]);
    setChatTraceId('');
    setChatLastEventId(0);
    setChatStatus('idle');
    setChatInput('');
  }

  async function createProject(projectName = newProjectName) {
    const name = projectName.trim();
    if (!name) return setProjectError('请输入项目名称。');
    if (projects.some(project => project.name.toLowerCase() === name.toLowerCase())) return setProjectError('项目已存在。');
    setProjectError('');
    try {
      const result = await client.createProject(name, '', {}, deviceIdentity?.deviceId, operationKey('desktop-project-create'));
      const project = projectFromGateway(result.project);
      setProjects(current => [project, ...current.filter(item => item.id !== project.id)]);
      setActiveProjectId(project.id);
      setNewProjectName('');
      setSnapshot(await client.bootstrap());
    } catch (cause) {
      setProjectError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function mutateProject(action: 'rename' | 'archive' | 'delete') {
    const project = projects.find(item => item.id === activeProjectId);
    if (!project) return;
    try {
      if (action === 'delete') await client.deleteProject(project.id, operationKey('desktop-project-delete'));
      else {
        const payload = action === 'archive'
          ? { archived: !project.archived }
          : { name: window.prompt('新项目名称', project.name)?.trim() };
        if (action === 'rename' && !payload.name) return;
        await client.updateProject(project.id, payload, operationKey(`desktop-project-${action}`));
      }
      await refreshProjects();
    } catch (cause) {
      setProjectError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  useEffect(() => {
    if (!token) return;
    const timer = window.setInterval(() => {
      void (autoWorkerEnabled ? runSafeWorkerCycle() : (async () => {
        const currentIdentity = await identity();
        await flushStatusOutbox(client);
        await client.heartbeat(currentIdentity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
        setSnapshot(await client.bootstrap());
      })()).catch(cause => setError(cause instanceof Error ? cause.message : String(cause)));
    }, autoWorkerEnabled ? 5000 : 15000);
    return () => window.clearInterval(timer);
  }, [client, token, claimedTask, autoWorkerEnabled]);

  const activeProject = projects.find(project => project.id === activeProjectId) || null;
  const pendingApprovals = snapshot?.pending_approvals || [];
  const notifications = snapshot?.notifications || [];

  return <main className="desktop-shell">
    <aside className="desktop-sidebar">
      <div className="traffic-row"><span className="dot red" /><span className="dot yellow" /><span className="dot green" /><strong>AI 助理 Desktop</strong></div>
      <nav className="desktop-nav">
        <button type="button" onClick={newConversation}>＋ 新对话</button>
        <button type="button" disabled title="全局搜索尚未启用">⌕ 搜索 · 未启用</button>
        <button type="button" disabled title="计划任务界面尚未启用">◴ 已安排 · 未启用</button>
        <button type="button" disabled title="插件管理界面尚未启用">✣ 插件 · 未启用</button>
      </nav>
      <section className="panel projects-panel">
        <div className="panel-title"><span>项目</span><button type="button" onClick={() => void createProject(newProjectName || '新项目')}>＋ 新建项目</button></div>
        <form className="project-form" onSubmit={event => { event.preventDefault(); void createProject(); }}><input value={newProjectName} onChange={event => setNewProjectName(event.target.value)} placeholder="输入项目名称后创建" /><button type="submit">创建</button></form>
        {projectError && <p className="error small-error">{projectError}</p>}
        <div className="project-list">{projects.length === 0 ? <div className="empty-state"><strong>暂无项目</strong><span>连接 Gateway 后加载组织项目。</span></div> : projects.map(project => <button className={`project-row ${project.id === activeProjectId ? 'active' : ''}`} key={project.id} type="button" onClick={() => setActiveProjectId(project.id)}><span>{initials(project.name)}</span><strong>{project.name}</strong></button>)}</div>
      </section>
      <button className="profile-row" type="button" onClick={() => setShowAccountSettings(open => !open)}><span className="avatar">{initials(actor)}</span><span><strong>{actor}</strong><small>Desktop operator</small></span></button>
    </aside>

    <section className="desktop-workspace">
      <header className="topbar"><button type="button" className="workspace-switcher" disabled title="使用左侧项目列表切换项目">▣ {activeProject?.name || '未选择项目'}</button><button type="button" onClick={connect}>▦ 连接 Gateway</button><button type="button" onClick={claim}>Claim Task</button><label className="worker-toggle"><input type="checkbox" checked={autoWorkerEnabled} onChange={event => setAutoWorkerEnabled(event.target.checked)} /> 自动 Worker</label></header>
      <section className="hero"><h1>Desktop Control Hub</h1><p>Provider 原生流式、停止生成、Lease、取消、恢复和受审批 Workspace 操作由同一 Gateway 管理。</p><section className="composer"><textarea value={chatInput} onChange={event => setChatInput(event.target.value)} placeholder="输入任务或问题" disabled={chatStreaming} /><div className="composer-actions"><button type="button" disabled title="附件上传尚未启用">＋ 附件 · 未启用</button><select value={chatProfile} onChange={event => setChatProfile(event.target.value)} disabled={chatStreaming}><option value="fast">快速</option><option value="planner">规划</option><option value="local">本地</option></select><button type="button" className="stop" onClick={stopChat} disabled={!chatStreaming}>■ 停止</button><button type="button" className="send" onClick={() => void askAssistant()} disabled={chatStreaming || !chatInput.trim()}>↑</button></div></section><p className="stream-status">Chat: {chatStatus} · Event {chatLastEventId} · Trace {chatTraceId || 'n/a'}</p>{error && <p className="error">{error}</p>}</section>
      <section className="quick-grid">{QUICK_ACTIONS.map(([icon, title, detail, prompt]) => <button className="quick-card" type="button" key={title} onClick={() => setChatInput(activeProject ? `[${activeProject.name}] ${prompt}` : prompt)}><span>{icon}</span><strong>{title}</strong><small>{detail}</small></button>)}</section>
      <section className="grid compact">{controlHubPanels.map(panel => <div className={`panel hub ${panel.status}`} key={panel.id}><h2>{panel.title}</h2><strong>{panel.status}</strong><p>{panel.count}</p></div>)}</section>
      <section className="grid data-grid"><div className="panel"><h2>Claimed Task</h2><pre>{JSON.stringify(claimedTask || {}, null, 2)}</pre><button disabled={!claimedTask} onClick={() => void complete('completed')}>标记完成</button>{' '}<button disabled={!claimedTask} onClick={() => void complete('failed')}>标记失败</button><h3>Worker log</h3><pre>{workerLog.join('\n')}</pre></div><div className="panel"><h2>最近对话</h2><div className="message-list">{chatMessages.slice(-8).map((message, index) => <article key={String(message.message_id || index)} className={`message ${message.role || ''}`}><strong>{String(message.role || 'message')}</strong><p>{String(message.content || '')}</p>{message.reasoning && <details><summary>Reasoning stream</summary><p>{String(message.reasoning)}</p></details>}</article>)}</div></div><div className="panel"><h2>Runtime</h2><pre>{JSON.stringify(snapshot?.runtime_status || [], null, 2)}</pre></div></section>
    </section>

    <aside className="desktop-rightbar">
      {showAccountSettings && <section className="panel account-panel"><div className="panel-title"><h2>账户设置</h2><span>未启用项已禁用</span></div>{ACCOUNT_SETTINGS.map(([title, detail]) => <button className="setting-row" key={title} type="button" disabled title="该设置尚未接入 Gateway"><span><strong>{title}</strong><small>{detail}</small></span><em>未启用</em></button>)}</section>}
      <section className="panel"><div className="panel-title"><h2>当前项目</h2><span>{activeProject ? (activeProject.archived ? 'archived' : 'ready') : '待创建'}</span></div><p>{activeProject ? `${activeProject.name} 已同步。` : '请连接 Gateway 并选择项目。'}</p>{activeProject && <p><button type="button" onClick={() => void mutateProject('rename')}>重命名</button>{' '}<button type="button" onClick={() => void mutateProject('archive')}>{activeProject.archived ? '恢复' : '归档'}</button>{' '}<button type="button" onClick={() => void mutateProject('delete')}>删除</button></p>}<p>Device: {deviceIdentity?.deviceId || 'not enrolled'}</p><p>Capabilities: {CAPABILITIES.join(', ')}</p></section>
      <section className="panel"><h2>连接配置</h2><label>Gateway URL<input value={gatewayUrl} onChange={event => setGatewayUrl(event.target.value)} /></label><label>Operator Token<input type="password" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" /></label><label>Actor<input value={actor} onChange={event => setActor(event.target.value)} /></label></section>
      <section className="panel"><h2>审批状态</h2><p>待审批：{pendingApprovals.length}</p><pre>{JSON.stringify(pendingApprovals.slice(0, 3), null, 2)}</pre></section>
      <section className="panel"><h2>通知</h2><p>{notifications.length} 条</p><pre>{JSON.stringify(notifications.slice(0, 5), null, 2)}</pre></section>
    </aside>

    <style>{`
      body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif;background:#08101a;color:#f6f8ff;overflow:hidden}button,input,textarea,select{font:inherit}button{cursor:pointer;color:inherit}button:disabled{opacity:.48;cursor:not-allowed}.desktop-shell{width:100vw;height:100vh;display:grid;grid-template-columns:292px minmax(620px,1fr) 336px;overflow:hidden}.desktop-sidebar,.desktop-rightbar{background:rgba(7,14,25,.8);overflow:auto;padding:16px}.desktop-sidebar{border-right:1px solid #263246;display:flex;flex-direction:column;gap:14px}.desktop-rightbar{border-left:1px solid #263246}.desktop-workspace{overflow:auto}.traffic-row,.topbar{display:flex;align-items:center;gap:10px}.dot{width:12px;height:12px;border-radius:50%}.red{background:#ff5f57}.yellow{background:#ffbd2e}.green{background:#28c840}.desktop-nav,.project-list{display:grid;gap:7px}.desktop-nav button,.workspace-switcher,.topbar button,.worker-toggle{min-height:40px;border:1px solid #34415a;background:#121e30;border-radius:13px;padding:0 13px;text-align:left}.panel,.profile-row,.composer,.quick-card{border:1px solid #2d3a51;background:linear-gradient(145deg,#172235,#0a1320);border-radius:18px;padding:16px}.panel-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;color:#a8b5ca}.projects-panel{flex:1}.project-form{display:grid;grid-template-columns:1fr auto;gap:8px}.project-row{display:grid;grid-template-columns:32px 1fr;align-items:center;border:0;background:transparent;padding:9px;border-radius:12px;text-align:left}.project-row.active{background:#354a9a}.profile-row{display:grid;grid-template-columns:42px 1fr;gap:10px;text-align:left}.avatar{width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:#5269ff}.topbar{height:70px;padding:0 22px;border-bottom:1px solid #263246;position:sticky;top:0;background:#0a1320;z-index:3}.worker-toggle{display:inline-flex;align-items:center;gap:8px}.worker-toggle input{width:auto}.hero{max-width:920px;margin:44px auto 24px;text-align:center}.hero h1{font-size:42px}.composer{text-align:left}.composer textarea,input,select{box-sizing:border-box;width:100%;padding:10px;border-radius:11px;border:1px solid #34415a;background:#101a2a;color:white}.composer-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:10px}.composer-actions select{width:auto}.send{width:44px;border-radius:50%;background:#5269ff}.stop{background:#7d3040}.stream-status{color:#a9b8ff;font-size:13px}.quick-grid,.grid{max-width:920px;margin:0 auto 18px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.quick-card{text-align:left;min-height:120px;display:grid;gap:8px}.compact{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.data-grid{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}.setting-row{width:100%;display:grid;grid-template-columns:1fr auto;text-align:left;align-items:center;border:1px solid #2d3a51;background:#101a2a;border-radius:13px;padding:10px;margin-bottom:8px}.setting-row em{font-style:normal}.error{color:#ff9aad}small{color:#93a1b5}pre{overflow:auto;max-height:320px;color:#d8dee9}.message-list{display:grid;gap:10px;max-height:420px;overflow:auto}.message{border:1px solid #33415a;border-radius:12px;padding:10px;white-space:pre-wrap}.message.user{background:#1b3152}.message.assistant{background:#111d2e}
    `}</style>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
