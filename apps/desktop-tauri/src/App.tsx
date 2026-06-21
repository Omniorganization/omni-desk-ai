import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { OmniApiClient } from './api';
import { buildControlHubPanels } from './controlHub';
import { executeRuntimeTask } from './executor';
import { createDesktopDeviceRequestSigner, loadOrCreateDesktopIdentity, DesktopDeviceIdentity } from './deviceIdentity';

const DEFAULT_GATEWAY = 'http://127.0.0.1:18789';
const VERSION = '1.12.3';
const CAPABILITIES = ['chat', 'local-runtime', 'browser', 'files', 'ui-bridge', 'sandbox'];

async function keychainGet(key: string): Promise<string> {
  try { return await invoke<string>('secure_get', { key }); } catch { return ''; }
}

async function keychainSet(key: string, value: string): Promise<void> {
  await invoke('secure_set', { key, value });
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

  async function connect() {
    setError('');
    await keychainSet('omni.gateway', gatewayUrl);
    await keychainSet('omni.operatorToken', token);
    await keychainSet('omni.actor', actor);
    const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
    setDeviceIdentity(identity);
    await client.registerDesktop(identity.deviceId, navigator.platform, CAPABILITIES, identity.publicKeyPem);
    await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);
    setSnapshot(await client.bootstrap());
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

  return <main className="shell">
    <section className="card">
      <h1>Omni Desktop Runtime</h1>
      <p>本地 Agent 运行器：Token 与设备私钥通过系统 Keychain / Credential Manager / Secret Service 保存，不写入 localStorage；device_id 每次安装独立生成。</p>
      <p>Device: {deviceIdentity?.deviceId || 'not enrolled'}</p>
      <label>Gateway URL<input value={gatewayUrl} onChange={e => setGatewayUrl(e.target.value)} /></label>
      <label>Operator Token<input type="password" value={token} onChange={e => setToken(e.target.value)} autoComplete="off" /></label>
      <label>Actor<input value={actor} onChange={e => setActor(e.target.value)} /></label>
      <button onClick={connect}>连接 Omni Gateway</button>{' '}<button onClick={claim}>Claim Desktop Task</button>{' '}<label className="inline"><input type="checkbox" checked={autoWorkerEnabled} onChange={e => setAutoWorkerEnabled(e.target.checked)} /> 自动 Worker</label>
      {error && <p className="error">{error}</p>}
    </section>

    <section className="grid compact">
      {controlHubPanels.map(panel => <div className={`card hub ${panel.status}`} key={panel.id}>
        <h2>{panel.title}</h2>
        <strong>{panel.status}</strong>
        <p>{panel.count}</p>
      </div>)}
    </section>

    <section className="card">
      <h2>Local Assistant</h2>
      <label>Ask Mode
        <select value={chatProfile} onChange={e => setChatProfile(e.target.value)}>
          <option value="fast">fast</option>
          <option value="planner">planner</option>
          <option value="local">local</option>
        </select>
      </label>
      <label>Message<textarea value={chatInput} onChange={e => setChatInput(e.target.value)} /></label>
      <button onClick={askAssistant}>问一下 AI</button>
      <pre>{JSON.stringify(chatMessages.map(message => ({
        role: message.role,
        content: message.content,
        provider: message.model_provider,
        model: message.model_name,
        profile: message.model_profile,
        audit_trace_id: message.trace_id
      })), null, 2)}</pre>
    </section>

    <section className="grid">
      <div className="card"><h2>Claimed Task</h2><pre>{JSON.stringify(claimedTask || {}, null, 2)}</pre><button disabled={!claimedTask} onClick={() => complete('completed')}>标记完成</button>{' '}<button disabled={!claimedTask} onClick={() => complete('failed')}>标记失败</button><h3>Worker log</h3><pre>{workerLog.join('\n')}</pre></div>
      <div className="card"><h2>Runtime</h2><pre>{JSON.stringify(snapshot?.runtime_status || [], null, 2)}</pre></div>
      <div className="card"><h2>Pending approvals</h2><pre>{JSON.stringify(snapshot?.pending_approvals || [], null, 2)}</pre></div>
      <div className="card"><h2>Notifications</h2><pre>{JSON.stringify(snapshot?.notifications || [], null, 2)}</pre></div>
    </section>
    <style>{`
      body { margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif; background: #101114; color: #f4f4f5; }
      .shell { padding: 24px; }
      .card { background: #181a20; border: 1px solid #30323a; border-radius: 16px; padding: 20px; margin-bottom: 16px; }
      label { display: block; margin: 12px 0; color: #c5c7ce; }
      input, textarea, select { width: 100%; margin-top: 6px; padding: 10px; border-radius: 10px; border: 1px solid #3a3d46; background: #0f1115; color: white; }
      textarea { min-height: 92px; resize: vertical; }
      button { padding: 10px 14px; border-radius: 10px; border: 0; cursor: pointer; }
      button:disabled { opacity: .45; cursor: not-allowed; }
      .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
      .compact { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
      .hub h2 { font-size: 14px; margin: 0 0 10px; }
      .hub strong { text-transform: uppercase; font-size: 12px; }
      .hub p { font-size: 24px; margin: 8px 0 0; }
      .hub.passed { border-color: #3b7f5f; }
      .hub.blocked { border-color: #925052; }
      .hub.pending { border-color: #88774c; }
      pre { overflow: auto; max-height: 360px; color: #d8dee9; }
      .error { color: #ff8888; }
      .inline { display: inline-flex; gap: 8px; align-items: center; margin-left: 12px; }
    `}</style>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
