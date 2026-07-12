#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if old not in text:
        raise RuntimeError(f"pattern missing in {path}: {old[:100]!r}")
    write(path, text.replace(old, new, 1))


# Edge-compatible nonce generation.
replace_once(
    "apps/web-admin-next/middleware.ts",
    "  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');",
    "  const nonce = btoa(crypto.randomUUID());",
)

# ---------------------------------------------------------------------------
# Web Admin: consume SSE, expose stop-generation, and disable unsupported rows.
# ---------------------------------------------------------------------------
web_path = "apps/web-admin-next/app/page.tsx"
web = read(web_path)
web = web.replace("import { useEffect, useState } from 'react';", "import { useEffect, useRef, useState } from 'react';", 1)
web = web.replace(
    "  const [chatMessages, setChatMessages] = useState<GatewayRecord[]>([]);",
    "  const [chatMessages, setChatMessages] = useState<GatewayRecord[]>([]);\n  const [streaming, setStreaming] = useState(false);\n  const [streamText, setStreamText] = useState('');\n  const [streamLastEventId, setStreamLastEventId] = useState(0);\n  const streamController = useRef<AbortController | null>(null);",
    1,
)
old_ask = '''  async function askAssistant() {
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
  }'''
new_ask = '''  function stopGeneration() {
    streamController.current?.abort();
    streamController.current = null;
    setStreaming(false);
  }

  async function askAssistant() {
    if (!canAsk) {
      setError('当前角色不是 operator/owner，不能发起模型问答。');
      return;
    }
    const content = chatInput.trim();
    if (!content || streaming) return;
    setError('');
    setLoading(true);
    setStreaming(true);
    setStreamText('');
    setStreamLastEventId(0);
    const controller = new AbortController();
    streamController.current = controller;
    let receivedStreamEvent = false;
    try {
      const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
      const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
      const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
      let conversationId = chatConversationId;
      await activeApi.streamChat(content, {
        conversationId: conversationId || undefined,
        modelProfile: chatProfile,
        signal: controller.signal,
        idempotencyKey: operationKey('web-admin-stream'),
        onEvent: (event) => {
          receivedStreamEvent = true;
          setStreamLastEventId(event.id);
          if (event.event === 'chat.started') {
            const resolved = String(event.data.conversation_id || '');
            if (resolved) {
              conversationId = resolved;
              setChatConversationId(resolved);
            }
          } else if (event.event === 'chat.delta') {
            setStreamText((current) => current + String(event.data.text || ''));
          } else if (event.event === 'chat.failed') {
            setError(`流式问答失败：${String(event.data.code || 'stream_failed')}`);
          }
        },
      });
      if (conversationId) {
        const messages = (await activeApi.listMessages(conversationId)).messages;
        setChatMessages(asRecordArray(messages));
      }
      setSnapshot(await activeApi.bootstrap());
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        try {
          if (!receivedStreamEvent) {
            const session = csrfToken ? { csrfToken, actor, role } : await establishSession();
            const identity = deviceIdentity || await ensureWebAdminDevice(session.csrfToken, session.actor, session.role);
            const activeApi = webAdminApiFor(identity, session.csrfToken, session.actor, session.role);
            let conversationId = chatConversationId;
            if (!conversationId) {
              const created = await activeApi.createConversation('Web Admin Chat');
              conversationId = String(created.conversation.conversation_id);
              setChatConversationId(conversationId);
            }
            await activeApi.askConversation(conversationId, content, chatProfile);
            setChatMessages(asRecordArray((await activeApi.listMessages(conversationId)).messages));
          } else {
            setError(e.message || String(e));
          }
        } catch (fallbackError: any) {
          setError(fallbackError.message || String(fallbackError));
        }
      }
    } finally {
      streamController.current = null;
      setStreaming(false);
      setLoading(false);
    }
  }'''
if old_ask not in web:
    raise RuntimeError("web askAssistant block not found")
web = web.replace(old_ask, new_ask, 1)
web = web.replace(
    "<div className=\"composer-actions\"><select value={chatProfile}",
    "<div className=\"composer-actions\"><span className=\"stream-state\">{streaming ? `Streaming · event ${streamLastEventId}` : 'Ready'}</span><select value={chatProfile}",
    1,
)
web = web.replace(
    "<button className=\"send-button\" type=\"button\" onClick={askAssistant} disabled={!canAsk || loading}>↑</button>",
    "{streaming ? <button className=\"send-button\" type=\"button\" onClick={stopGeneration}>■</button> : <button className=\"send-button\" type=\"button\" onClick={askAssistant} disabled={!canAsk || loading}>↑</button>}",
    1,
)
web = web.replace(
    "        {error && <div className=\"error-banner\">{error}</div>}",
    "        {streaming && <div className=\"stream-preview\" aria-live=\"polite\">{streamText || '正在连接模型…'}</div>}\n        {error && <div className=\"error-banner\">{error}</div>}",
    1,
)
web = web.replace(
    "{ACCOUNT_SETTINGS.map((setting) => <button className=\"setting-row\" key={setting.title} type=\"button\"><span>",
    "{ACCOUNT_SETTINGS.map((setting) => <button className=\"setting-row\" key={setting.title} type=\"button\" disabled title=\"未启用\"><span>",
    1,
)
web = web.replace("<em>{setting.action}</em>", "<em>未启用</em>", 1)
write(web_path, web)

# ---------------------------------------------------------------------------
# Desktop: SSE, cancellation, lease renewal, timeout, recovery/outbox, truth UI.
# ---------------------------------------------------------------------------
write(
    "apps/desktop-tauri/src/runtimeOutbox.ts",
    '''import type { OmniApiClient, TaskStatus } from './api';

export interface PendingTaskStatus {
  taskId: string;
  status: TaskStatus;
  summary: string;
  deviceId?: string;
  key: string;
  createdAt: string;
}

const OUTBOX_KEY = 'omnidesk.desktop.runtime-status-outbox.v1';
const ACTIVE_TASK_KEY = 'omnidesk.desktop.active-task.v1';

function readOutbox(): PendingTaskStatus[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeOutbox(items: PendingTaskStatus[]): void {
  localStorage.setItem(OUTBOX_KEY, JSON.stringify(items.slice(-100)));
}

export function rememberActiveTask(task: Record<string, unknown>): void {
  localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify(task));
}

export function clearActiveTask(): void {
  localStorage.removeItem(ACTIVE_TASK_KEY);
}

export function recoveredActiveTask(): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(ACTIVE_TASK_KEY) || 'null');
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export async function reportTaskStatus(
  client: OmniApiClient,
  item: PendingTaskStatus,
): Promise<void> {
  try {
    await client.updateTaskStatus(item.taskId, item.status, item.summary, item.deviceId, item.key);
  } catch {
    const existing = readOutbox().filter((candidate) => candidate.key !== item.key);
    writeOutbox([...existing, item]);
    throw new Error('task status queued for retry');
  }
}

export async function flushTaskStatusOutbox(client: OmniApiClient): Promise<number> {
  const pending = readOutbox();
  const remaining: PendingTaskStatus[] = [];
  let sent = 0;
  for (const item of pending) {
    try {
      await client.updateTaskStatus(item.taskId, item.status, item.summary, item.deviceId, item.key);
      sent += 1;
    } catch {
      remaining.push(item);
    }
  }
  writeOutbox(remaining);
  return sent;
}
''',
)

app_path = "apps/desktop-tauri/src/App.tsx"
app = read(app_path)
app = app.replace(
    "import { createDesktopDeviceRequestSigner, loadOrCreateDesktopIdentity, DesktopDeviceIdentity } from './deviceIdentity';",
    "import { createDesktopDeviceRequestSigner, loadOrCreateDesktopIdentity, DesktopDeviceIdentity } from './deviceIdentity';\nimport { clearActiveTask, flushTaskStatusOutbox, recoveredActiveTask, rememberActiveTask, reportTaskStatus } from './runtimeOutbox';",
    1,
)
app = app.replace(
    "  const workerBusy = useRef(false);",
    "  const workerBusy = useRef(false);\n  const streamController = useRef<AbortController | null>(null);",
    1,
)
app = app.replace(
    "  const [chatMessages, setChatMessages] = useState<any[]>([]);",
    "  const [chatMessages, setChatMessages] = useState<any[]>([]);\n  const [streaming, setStreaming] = useState(false);\n  const [streamText, setStreamText] = useState('');\n  const [streamLastEventId, setStreamLastEventId] = useState(0);",
    1,
)
# Connect flushes status outbox and reports interrupted prior task as recovery-required.
app = app.replace(
    "      await signedClient.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);\n      setSnapshot(await signedClient.bootstrap());",
    "      await signedClient.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, claimedTask?.task_id);\n      await flushTaskStatusOutbox(signedClient);\n      const interrupted = recoveredActiveTask();\n      if (interrupted?.task_id) {\n        await reportTaskStatus(signedClient, {\n          taskId: String(interrupted.task_id),\n          status: 'failed',\n          summary: 'Desktop restarted during execution; recovery required before retry.',\n          deviceId: identity.deviceId,\n          key: `desktop-recovery-${String(interrupted.task_id)}-${String(interrupted.attempt_id || 'unknown')}`,\n          createdAt: new Date().toISOString(),\n        }).catch(() => undefined);\n        clearActiveTask();\n      }\n      setSnapshot(await signedClient.bootstrap());",
    1,
)
old_cycle = '''  async function runSafeWorkerCycle() {
    if (workerBusy.current) return;
    workerBusy.current = true;
    try {
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
    } finally {
      workerBusy.current = false;
    }
  }'''
new_cycle = '''  async function runSafeWorkerCycle() {
    if (workerBusy.current) return;
    workerBusy.current = true;
    let leaseTimer: number | undefined;
    const executionController = new AbortController();
    try {
      const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
      setDeviceIdentity(identity);
      await flushTaskStatusOutbox(client);
      const result = await client.claimTask(identity.deviceId, CAPABILITIES, 120);
      const task = result.task || null;
      if (!task) {
        await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES);
        return;
      }
      setClaimedTask(task);
      rememberActiveTask(task);
      await client.heartbeat(identity.deviceId, 'online', VERSION, CAPABILITIES, task.task_id);
      const leaseToken = String(task.lease_token || '');
      if (leaseToken) {
        leaseTimer = window.setInterval(() => {
          void client.renewTaskLease(task.task_id, identity.deviceId, leaseToken, 120).catch(() => {
            executionController.abort('lease renewal failed');
          });
        }, 45_000);
      }
      const timeoutMs = Math.max(1_000, Math.min(Number(task.timeout_seconds || 120) * 1_000, 15 * 60_000));
      const timeout = new Promise<never>((_, reject) => {
        window.setTimeout(() => {
          executionController.abort('task timeout');
          reject(new Error('task execution timed out'));
        }, timeoutMs);
      });
      const execution = await Promise.race([executeRuntimeTask(task, executionController.signal), timeout]);
      const report = {
        taskId: task.task_id,
        status: execution.status,
        summary: execution.summary,
        deviceId: identity.deviceId,
        key: `desktop-task-result-${task.task_id}-${String(task.attempt_id || task.attempt_count || '1')}`,
        createdAt: new Date().toISOString(),
      } as const;
      await reportTaskStatus(client, report).catch(() => undefined);
      setWorkerLog(items => [`${new Date().toISOString()} ${execution.status}: ${execution.summary}`, ...items].slice(0, 20));
      clearActiveTask();
      setClaimedTask(null);
      setSnapshot(await client.bootstrap());
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'runtime cycle failed';
      setWorkerLog(items => [`${new Date().toISOString()} failed: ${message}`, ...items].slice(0, 20));
      throw cause;
    } finally {
      if (leaseTimer !== undefined) window.clearInterval(leaseTimer);
      executionController.abort();
      workerBusy.current = false;
    }
  }'''
if old_cycle not in app:
    raise RuntimeError("desktop worker cycle block not found")
app = app.replace(old_cycle, new_cycle, 1)
old_desktop_ask = '''  async function askAssistant() {
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
  }'''
new_desktop_ask = '''  function stopGeneration() {
    streamController.current?.abort();
    streamController.current = null;
    setStreaming(false);
  }

  async function askAssistant() {
    if (streaming || !chatInput.trim()) return;
    setError('');
    setStreaming(true);
    setStreamText('');
    setStreamLastEventId(0);
    const controller = new AbortController();
    streamController.current = controller;
    let received = false;
    try {
      const identity = deviceIdentity || await loadOrCreateDesktopIdentity();
      setDeviceIdentity(identity);
      let conversationId = chatConversationId;
      await client.streamChat(chatInput, {
        conversationId: conversationId || undefined,
        modelProfile: chatProfile,
        sourceDeviceId: identity.deviceId,
        signal: controller.signal,
        idempotencyKey: operationKey('desktop-stream'),
        onEvent: (event) => {
          received = true;
          setStreamLastEventId(event.id);
          if (event.event === 'chat.started') {
            const resolved = String(event.data.conversation_id || '');
            if (resolved) {
              conversationId = resolved;
              setChatConversationId(resolved);
            }
          } else if (event.event === 'chat.delta') {
            setStreamText((current) => current + String(event.data.text || ''));
          } else if (event.event === 'chat.failed') {
            setError(`stream failed: ${String(event.data.code || 'stream_failed')}`);
          }
        },
      });
      if (conversationId) setChatMessages((await client.listMessages(conversationId)).messages || []);
      setSnapshot(await client.bootstrap());
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        if (!received) {
          try {
            let conversationId = chatConversationId;
            if (!conversationId) {
              const created = await client.createConversation('Desktop Ask Mode', deviceIdentity?.deviceId);
              conversationId = created.conversation.conversation_id;
              setChatConversationId(conversationId);
            }
            await client.askConversation(conversationId, chatInput, chatProfile, deviceIdentity?.deviceId);
            setChatMessages((await client.listMessages(conversationId)).messages || []);
          } catch (fallbackError: any) {
            setError(fallbackError.message || String(fallbackError));
          }
        } else {
          setError(e.message || String(e));
        }
      }
    } finally {
      streamController.current = null;
      setStreaming(false);
    }
  }'''
if old_desktop_ask not in app:
    raise RuntimeError("desktop ask block not found")
app = app.replace(old_desktop_ask, new_desktop_ask, 1)
app = app.replace('<button type="button">⌕ 搜索</button>', '<button type="button" disabled title="未启用">⌕ 搜索 · 未启用</button>', 1)
app = app.replace('<button type="button">◴ 已安排</button>', '<button type="button" disabled title="未启用">◴ 已安排 · 未启用</button>', 1)
app = app.replace('<button type="button">✣ 插件</button>', '<button type="button" disabled title="未启用">✣ 插件 · 未启用</button>', 1)
app = app.replace('<button type="button" className="workspace-switcher">▣ {activeProjectName} ⌄</button>', '<button type="button" className="workspace-switcher" disabled title="项目切换器未启用">▣ {activeProjectName} ⌄</button>', 1)
app = app.replace('<div className="composer-actions"><button type="button">＋ 附件</button>', '<div className="composer-actions"><button type="button" disabled title="附件未启用">＋ 附件 · 未启用</button>', 1)
app = app.replace('<button type="button" className="send" onClick={askAssistant}>↑</button>', '{streaming ? <button type="button" className="send" onClick={stopGeneration}>■</button> : <button type="button" className="send" onClick={askAssistant}>↑</button>}', 1)
app = app.replace('{error && <p className="error">{error}</p>}', '{streaming && <p className="stream-preview" aria-live="polite">{streamText || `Connecting… event ${streamLastEventId}`}</p>}{error && <p className="error">{error}</p>}', 1)
app = app.replace('type="button"><span><strong>{setting.title}</strong>', 'type="button" disabled title="未启用"><span><strong>{setting.title}</strong>', 1)
app = app.replace('<em>{setting.action}</em>', '<em>未启用</em>', 1)
write(app_path, app)

# Executor accepts cancellation checks around every native boundary.
executor_path = "apps/desktop-tauri/src/executor.ts"
executor = read(executor_path)
executor = executor.replace(
    "export interface RuntimeExecutor { capability: RuntimeCapability; canExecute(task: RuntimeTask): boolean; execute(task: RuntimeTask): Promise<ExecutionResult>; }",
    "export interface RuntimeExecutor { capability: RuntimeCapability; canExecute(task: RuntimeTask): boolean; execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult>; }",
    1,
)
executor = executor.replace("async execute(task: RuntimeTask): Promise<ExecutionResult>", "async execute(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult>")
executor = executor.replace("    requireApprovalScope(task);", "    if (signal?.aborted) throw new Error('runtime execution cancelled');\n    requireApprovalScope(task);")
executor = executor.replace("      const output = await invoke<string>('read_workspace_file'", "      const output = await invoke<string>('read_workspace_file'", 1)
executor = executor.replace("      return { status: 'completed', summary: `workspace read completed", "      if (signal?.aborted) throw new Error('runtime execution cancelled');\n      return { status: 'completed', summary: `workspace read completed", 1)
executor = executor.replace("      return { status: 'completed', summary: `workspace list completed", "      if (signal?.aborted) throw new Error('runtime execution cancelled');\n      return { status: 'completed', summary: `workspace list completed", 1)
executor = executor.replace("      return { status: 'completed', summary: `workspace write completed", "      if (signal?.aborted) throw new Error('runtime execution cancelled');\n      return { status: 'completed', summary: `workspace write completed", 1)
executor = executor.replace("      return { status: 'completed', summary: 'workspace delete completed", "      if (signal?.aborted) throw new Error('runtime execution cancelled');\n      return { status: 'completed', summary: 'workspace delete completed", 1)
executor = executor.replace(
    "export async function executeRuntimeTask(task: RuntimeTask): Promise<ExecutionResult> {",
    "export async function executeRuntimeTask(task: RuntimeTask, signal?: AbortSignal): Promise<ExecutionResult> {",
    1,
)
executor = executor.replace("    return await executor.execute(task);", "    return await executor.execute(task, signal);", 1)
write(executor_path, executor)

# ---------------------------------------------------------------------------
# Mobile: actual stream subscription, stop button, and final refresh.
# ---------------------------------------------------------------------------
mobile_path = "apps/mobile-flutter/lib/main.dart"
mobile = read(mobile_path)
mobile = mobile.replace("import 'package:flutter/material.dart';", "import 'dart:async';\n\nimport 'package:flutter/material.dart';", 1)
mobile = mobile.replace(
    "  List<dynamic> chatMessages = <dynamic>[];",
    "  List<dynamic> chatMessages = <dynamic>[];\n  StreamSubscription<ChatStreamEvent>? chatStreamSubscription;\n  bool streaming = false;\n  String streamText = '';\n  int streamLastEventId = 0;",
    1,
)
mobile = mobile.replace(
    "    reasonController.dispose();\n    super.dispose();",
    "    reasonController.dispose();\n    chatStreamSubscription?.cancel();\n    super.dispose();",
    1,
)
old_mobile_ask = '''  Future<void> askAssistant() async {
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
  }'''
new_mobile_ask = '''  Future<void> stopGeneration() async {
    await chatStreamSubscription?.cancel();
    chatStreamSubscription = null;
    if (mounted) setState(() => streaming = false);
  }

  Future<void> askAssistant() async {
    if (streaming || taskController.text.trim().isEmpty) return;
    try {
      final identity = deviceIdentity ?? await identityStore.loadOrCreate();
      deviceIdentity = identity;
      var conversationId = chatConversationId;
      setState(() {
        streaming = true;
        streamText = '';
        streamLastEventId = 0;
        error = '';
      });
      final stream = client.streamChat(
        taskController.text,
        conversationId: conversationId,
        modelProfile: chatProfile,
        sourceDeviceId: identity.deviceId,
        idempotencyKey: _operationKey('mobile-stream'),
      );
      chatStreamSubscription = stream.listen(
        (event) {
          if (!mounted) return;
          setState(() {
            streamLastEventId = event.id;
            if (event.event == 'chat.started') {
              final resolved = event.data['conversation_id']?.toString() ?? '';
              if (resolved.isNotEmpty) {
                conversationId = resolved;
                chatConversationId = resolved;
              }
            } else if (event.event == 'chat.delta') {
              streamText += event.data['text']?.toString() ?? '';
            } else if (event.event == 'chat.failed') {
              error = '流式问答失败：${event.data['code'] ?? 'stream_failed'}';
            }
          });
        },
        onError: (Object cause) async {
          if (!mounted) return;
          try {
            if (streamText.isEmpty) {
              var fallbackConversation = conversationId;
              if (fallbackConversation == null || fallbackConversation!.isEmpty) {
                final conv = await client.createConversation('Mobile Ask Mode');
                fallbackConversation = conv['conversation']['conversation_id'] as String;
                chatConversationId = fallbackConversation;
              }
              await client.askConversation(
                fallbackConversation,
                taskController.text,
                modelProfile: chatProfile,
                sourceDeviceId: identity.deviceId,
              );
            } else {
              error = cause.toString();
            }
          } finally {
            if (mounted) setState(() => streaming = false);
          }
        },
        onDone: () async {
          if (conversationId != null && conversationId!.isNotEmpty) {
            final messages = await client.listMessages(conversationId!);
            chatMessages = messages['messages'] as List<dynamic>? ?? <dynamic>[];
          }
          snapshot = await client.bootstrap();
          chatStreamSubscription = null;
          if (mounted) setState(() => streaming = false);
        },
        cancelOnError: false,
      );
    } catch (e) {
      if (mounted) setState(() {
        streaming = false;
        error = e.toString();
      });
    }
  }'''
if old_mobile_ask not in mobile:
    raise RuntimeError("mobile ask block not found")
mobile = mobile.replace(old_mobile_ask, new_mobile_ask, 1)
mobile = mobile.replace(
    "                FilledButton.icon(\n                  onPressed: () {\n                    askAssistant();\n                  },\n                  icon: const Icon(Icons.arrow_upward),\n                  label: const Text('问一下 AI'),\n                ),",
    "                if (!streaming)\n                  FilledButton.icon(\n                    onPressed: askAssistant,\n                    icon: const Icon(Icons.arrow_upward),\n                    label: const Text('问一下 AI'),\n                  )\n                else\n                  FilledButton.icon(\n                    onPressed: stopGeneration,\n                    icon: const Icon(Icons.stop),\n                    label: Text('停止生成 · event $streamLastEventId'),\n                  ),",
    1,
)
# Insert live region beneath action row when exact marker is available.
mobile = mobile.replace(
    "            ],\n          ),\n        ),\n      ),\n    );\n  }\n\n  Widget _accountSettingsCard()",
    "            ],\n            if (streaming || streamText.isNotEmpty) ...<Widget>[\n              const SizedBox(height: 12),\n              SelectableText(streamText.isEmpty ? '正在连接模型…' : streamText),\n            ],\n          ),\n        ),\n      ),\n    );\n  }\n\n  Widget _accountSettingsCard()",
    1,
)
write(mobile_path, mobile)

# ---------------------------------------------------------------------------
# Task creation carries approved runtime scope/policies.
# ---------------------------------------------------------------------------
store = read("omnidesk_agent/appsync/store.py")
store = store.replace(
    "def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = \"medium\", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None)",
    "def add_message_and_task(self, *, actor: str, conversation_id: str, content: str, source_device_id: Optional[str] = None, requires_desktop_runtime: bool = False, risk: str = \"medium\", capability: Optional[str] = None, scope: dict[str, Any] | None = None, timeout_seconds: int = 120, artifact_policy: str = \"summary\", network_policy: str = \"none\", filesystem_policy: str = \"workspace_only\", idempotency_key: Optional[str] = None, idempotency_payload: dict[str, Any] | None = None)",
    1,
)
store = store.replace(
    "                idempotency_key=idempotency_key,\n            )",
    "                idempotency_key=idempotency_key,\n                capability=capability,\n                scope=dict(scope or {}),\n                timeout_seconds=max(1, min(int(timeout_seconds or 120), 900)),\n                artifact_policy=artifact_policy,\n                network_policy=network_policy,\n                filesystem_policy=filesystem_policy,\n            )",
    1,
)
write("omnidesk_agent/appsync/store.py", store)

routes = read("omnidesk_agent/appsync/routes.py")
routes = routes.replace(
    "                risk=payload.get(\"risk\") or \"medium\",\n                idempotency_key=_require_idempotency",
    "                risk=payload.get(\"risk\") or \"medium\",\n                capability=str(payload.get(\"capability\") or \"\") or None,\n                scope=payload.get(\"scope\") if isinstance(payload.get(\"scope\"), dict) else {},\n                timeout_seconds=int(payload.get(\"timeout_seconds\") or 120),\n                artifact_policy=str(payload.get(\"artifact_policy\") or \"summary\"),\n                network_policy=str(payload.get(\"network_policy\") or \"none\"),\n                filesystem_policy=str(payload.get(\"filesystem_policy\") or \"workspace_only\"),\n                idempotency_key=_require_idempotency",
    1,
)
write("omnidesk_agent/appsync/routes.py", routes)

mobile_api = read("apps/mobile-flutter/lib/omni_api.dart")
mobile_api = mobile_api.replace(
    "    String risk = 'medium',\n    String? idempotencyKey,",
    "    String risk = 'medium',\n    String? capability,\n    Map<String, dynamic>? scope,\n    int timeoutSeconds = 120,\n    String artifactPolicy = 'summary',\n    String networkPolicy = 'none',\n    String filesystemPolicy = 'workspace_only',\n    String? idempotencyKey,",
    1,
)
mobile_api = mobile_api.replace(
    "          'risk': risk,\n        },",
    "          'risk': risk,\n          if (capability != null && capability.isNotEmpty) 'capability': capability,\n          if (scope != null) 'scope': scope,\n          'timeout_seconds': timeoutSeconds,\n          'artifact_policy': artifactPolicy,\n          'network_policy': networkPolicy,\n          'filesystem_policy': filesystemPolicy,\n        },",
    1,
)
write("apps/mobile-flutter/lib/omni_api.dart", mobile_api)

# Tests for UI closure and worker lifecycle source contracts.
write(
    "tests/test_industrial_96_product_closure.py",
    '''from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_all_three_clients_consume_stream_and_expose_cancellation():
    assert "streamChat" in text("apps/web-admin-next/lib/api.ts")
    assert "stopGeneration" in text("apps/web-admin-next/app/page.tsx")
    assert "streamChat" in text("apps/desktop-tauri/src/api.ts")
    assert "stopGeneration" in text("apps/desktop-tauri/src/App.tsx")
    assert "Stream<ChatStreamEvent> streamChat" in text("apps/mobile-flutter/lib/omni_api.dart")
    assert "Future<void> stopGeneration" in text("apps/mobile-flutter/lib/main.dart")


def test_desktop_worker_has_lease_timeout_recovery_and_durable_reporting():
    app = text("apps/desktop-tauri/src/App.tsx")
    outbox = text("apps/desktop-tauri/src/runtimeOutbox.ts")
    assert "renewTaskLease" in app
    assert "task.timeout_seconds" in app
    assert "recoveredActiveTask" in app
    assert "flushTaskStatusOutbox" in app
    assert "localStorage" in outbox


def test_unsupported_controls_are_not_clickable():
    desktop = text("apps/desktop-tauri/src/App.tsx")
    web = text("apps/web-admin-next/app/page.tsx")
    assert "搜索 · 未启用" in desktop
    assert "附件 · 未启用" in desktop
    assert "title=\"未启用\"" in web


def test_task_contract_carries_runtime_scope_and_write_capability():
    store = text("omnidesk_agent/appsync/store.py")
    executor = text("apps/desktop-tauri/src/executor.ts")
    assert "lease_token" in store
    assert "renew_task_lease" in store
    assert "capability=capability" in store
    assert "class FileOperationExecutor" in executor
    assert "write_workspace_file" in executor
''',
)

print("industrial 96 product closure transformation completed")
