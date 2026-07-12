'use client';

import { useMemo, useRef, useState } from 'react';
import Link from 'next/link';

import { OmniAdminApi, type AdminRole, type ChatStreamEvent } from '@/lib/api';
import {
  loadOrCreateWebAdminIdentity,
  signWebAdminDeviceRequest,
  type WebAdminDeviceIdentity,
} from '@/lib/device-identity';

import styles from './StreamingWorkspace.module.css';

type DisplayMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string;
};

function operationKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export default function StreamingWorkspace() {
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:18789');
  const [token, setToken] = useState('');
  const [actor, setActor] = useState('web-admin');
  const [role, setRole] = useState<AdminRole>('operator');
  const [csrfToken, setCsrfToken] = useState('');
  const [identity, setIdentity] = useState<WebAdminDeviceIdentity | null>(null);
  const [conversationId, setConversationId] = useState('');
  const [prompt, setPrompt] = useState('分析当前项目并给出下一步执行计划。');
  const [profile, setProfile] = useState('fast');
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [status, setStatus] = useState('未连接');
  const [traceId, setTraceId] = useState('');
  const [lastEventId, setLastEventId] = useState(0);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const activeMessageIdRef = useRef('');

  const api = useMemo(
    () => new OmniAdminApi({
      csrfToken,
      actor,
      role,
      deviceId: identity?.deviceId,
      publicKeyPem: identity?.publicKeyPem,
      deviceSigner: signWebAdminDeviceRequest,
    }),
    [csrfToken, actor, role, identity],
  );

  async function connect() {
    setError('');
    setStatus('建立 Web Session');
    const response = await fetch('/api/session/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ gatewayUrl: baseUrl, token, actor, role }),
    });
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json() as Record<string, unknown>;
    const activeCsrf = String(body.csrfToken || '');
    const activeActor = String(body.actor || actor);
    const activeRole = String(body.role || role) as AdminRole;
    setCsrfToken(activeCsrf);
    setActor(activeActor);
    setRole(activeRole);

    const loadedIdentity = await loadOrCreateWebAdminIdentity();
    setIdentity(loadedIdentity);
    const activeApi = new OmniAdminApi({
      csrfToken: activeCsrf,
      actor: activeActor,
      role: activeRole,
      deviceId: loadedIdentity.deviceId,
      publicKeyPem: loadedIdentity.publicKeyPem,
      deviceSigner: signWebAdminDeviceRequest,
    });
    await activeApi.registerAdminDevice(loadedIdentity);
    setStatus(`已连接 · ${activeActor} · ${activeRole}`);
  }

  function updateAssistant(event: ChatStreamEvent) {
    setLastEventId(event.id);
    const activeId = activeMessageIdRef.current;
    if (event.event === 'chat.delta') {
      const text = String(event.data.text || '');
      setMessages(current => current.map(message => (
        message.id === activeId
          ? { ...message, content: `${message.content}${text}` }
          : message
      )));
    } else if (event.event === 'chat.reasoning.delta') {
      const reasoning = String(event.data.text || '');
      setMessages(current => current.map(message => (
        message.id === activeId
          ? { ...message, reasoning: `${message.reasoning || ''}${reasoning}` }
          : message
      )));
    } else if (event.event === 'chat.started') {
      const id = String(event.data.conversation_id || '');
      if (id) setConversationId(id);
      setStatus('模型流式生成中');
    } else if (event.event === 'chat.usage') {
      setStatus(`Usage: ${JSON.stringify(event.data)}`);
    } else if (event.event === 'chat.completed') {
      setTraceId(String(event.data.audit_trace_id || ''));
      setStatus(event.data.native === false ? '已完成 · 非流式兼容回退' : '已完成 · Provider 原生流式');
    } else if (event.event === 'chat.failed') {
      setStatus(`失败 · ${String(event.data.code || 'chat_stream_failed')}`);
    }
  }

  async function ensureConversation(activeApi: OmniAdminApi): Promise<string> {
    if (conversationId) return conversationId;
    const created = await activeApi.createConversation('Web Admin Streaming');
    const id = String(created.conversation?.conversation_id || '');
    if (!id) throw new Error('conversation_id_missing');
    setConversationId(id);
    return id;
  }

  async function send() {
    const content = prompt.trim();
    if (!content || running) return;
    setError('');
    setRunning(true);
    setLastEventId(0);
    const controller = new AbortController();
    abortRef.current = controller;
    const idempotencyKey = operationKey('web-stream');
    const assistantId = crypto.randomUUID();
    activeMessageIdRef.current = assistantId;
    setMessages(current => [
      ...current,
      { id: crypto.randomUUID(), role: 'user', content },
      { id: assistantId, role: 'assistant', content: '' },
    ]);

    try {
      const activeConversationId = await ensureConversation(api);
      let cursor = 0;
      let reconnects = 0;
      let observed = false;
      while (true) {
        try {
          const result = await api.streamChat({
            conversationId: activeConversationId,
            content,
            modelProfile: profile,
            idempotencyKey,
            lastEventId: cursor,
            signal: controller.signal,
            onEvent: event => {
              observed = true;
              cursor = event.id;
              updateAssistant(event);
            },
          });
          cursor = result.lastEventId;
          break;
        } catch (cause) {
          if (controller.signal.aborted) throw cause;
          if (observed && reconnects < 1) {
            reconnects += 1;
            setStatus(`连接中断，按 Event ID ${cursor} 恢复`);
            continue;
          }
          if (observed) throw cause;
          setStatus('流式建立失败，自动切换非流式问答');
          const fallback = await api.askConversation(
            activeConversationId,
            content,
            profile,
            idempotencyKey,
          );
          const assistant = fallback.assistant_message || {};
          setMessages(current => current.map(message => (
            message.id === assistantId
              ? { ...message, content: String(assistant.content || '') }
              : message
          )));
          setTraceId(String(fallback.audit_trace_id || assistant.trace_id || ''));
          setStatus('已完成 · 非流式自动降级');
          break;
        }
      }
      setLastEventId(cursor);
      setPrompt('');
    } catch (cause) {
      if (controller.signal.aborted) {
        setStatus('已停止生成');
      } else {
        setError(cause instanceof Error ? cause.message : String(cause));
        setStatus('生成失败');
      }
    } finally {
      abortRef.current = null;
      setRunning(false);
    }
  }

  function stop() {
    abortRef.current?.abort('operator_stop');
  }

  function newConversation() {
    stop();
    setConversationId('');
    setMessages([]);
    setTraceId('');
    setLastEventId(0);
    setStatus(csrfToken ? '已连接 · 新对话' : '未连接');
  }

  return <main className={styles.shell}>
    <header className={styles.header}>
      <div>
        <h1>Web Admin 流式工作区</h1>
        <p className={styles.meta}>SSE · Stop Generation · Last-Event-ID Resume · Non-streaming Fallback</p>
      </div>
      <Link href="/">返回控制台</Link>
    </header>

    <section className={styles.grid}>
      <aside className={styles.card}>
        <div className={styles.form}>
          <label>Gateway URL<input value={baseUrl} onChange={event => setBaseUrl(event.target.value)} /></label>
          <label>Session Token<input type="password" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" /></label>
          <label>Actor<input value={actor} onChange={event => setActor(event.target.value)} /></label>
          <label>Role<select value={role} onChange={event => setRole(event.target.value as AdminRole)}><option value="viewer">viewer</option><option value="operator">operator</option><option value="owner">owner</option></select></label>
          <div className={styles.actions}><button className={styles.primary} type="button" onClick={() => void connect().catch(cause => { setError(cause instanceof Error ? cause.message : String(cause)); setStatus('连接失败'); })} disabled={running}>连接并注册设备</button><button type="button" onClick={newConversation}>新对话</button></div>
          <p className={styles.status}>{status}</p>
          <p className={styles.meta}>Conversation: {conversationId || 'not created'}</p>
          <p className={styles.meta}>Last Event ID: {lastEventId}</p>
          <p className={styles.meta}>Trace: {traceId || 'n/a'}</p>
          {error && <p className={styles.error}>{error}</p>}
        </div>
      </aside>

      <section className={styles.card}>
        <div className={styles.messages} aria-live="polite">
          {messages.length === 0 && <p className={styles.meta}>建立 Session 后发送消息。流式连接中断会按 Event ID 自动恢复一次。</p>}
          {messages.map(message => <article className={`${styles.message} ${message.role === 'user' ? styles.user : styles.assistant}`} key={message.id}><strong>{message.role}</strong><p>{message.content || (message.role === 'assistant' && running ? '生成中…' : '')}</p>{message.reasoning && <details><summary className={styles.reasoning}>Reasoning stream</summary><p className={styles.reasoning}>{message.reasoning}</p></details>}</article>)}
        </div>
        <div className={styles.composer}>
          <textarea value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="输入问题" onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void send(); }} />
          <div className={styles.actions}><select value={profile} onChange={event => setProfile(event.target.value)}><option value="fast">fast</option><option value="planner">planner</option><option value="local">local</option></select><button className={styles.primary} type="button" onClick={() => void send()} disabled={running || !csrfToken || role === 'viewer' || !prompt.trim()}>开始生成</button><button className={styles.danger} type="button" onClick={stop} disabled={!running}>停止生成</button></div>
        </div>
      </section>
    </section>
  </main>;
}
