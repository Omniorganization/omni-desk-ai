'use client';

import { useMemo, useState } from 'react';
import { AdminRole, OmniAdminApi } from '../lib/api';

const ROLE_HELP: Record<AdminRole, string> = {
  viewer: '只读查看设备、运行器、通知与审计状态。',
  operator: '可发起普通操作并刷新运行状态。',
  owner: '可审批/拒绝高风险动作。'
};

export default function Page() {
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:18789');
  const [token, setToken] = useState('');
  const [actor, setActor] = useState('web-admin');
  const [role, setRole] = useState<AdminRole>('viewer');
  const [csrfToken, setCsrfToken] = useState('');
  const [decisionReason, setDecisionReason] = useState('Reviewed in Web Admin controlled-staging console');
  const [snapshot, setSnapshot] = useState<any>(null);
  const [ecosystem, setEcosystem] = useState<any>(null);
  const [error, setError] = useState('');
  const api = useMemo(() => new OmniAdminApi({ csrfToken, actor, role }), [csrfToken, actor, role]);
  const canApprove = role === 'owner';

  async function establishSession() {
    const response = await fetch('/api/session/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ gatewayUrl: baseUrl, token, actor, role })
    });
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json();
    setCsrfToken(body.csrfToken || '');
    return body.csrfToken || '';
  }

  async function load() {
    setError('');
    try {
      if (!csrfToken) await establishSession();
      await api.registerAdminDevice();
      setSnapshot(await api.bootstrap());
      setEcosystem(await api.ecosystem());
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  async function decide(id: string, decision: 'approved' | 'rejected') {
    if (!canApprove) {
      setError('当前角色不是 owner，不能执行审批。');
      return;
    }
    try {
      await api.decide(id, decision, decisionReason);
      setSnapshot(await api.bootstrap());
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  const approvals = snapshot?.pending_approvals || [];
  return <main>
    <section className="card">
      <h1>Omni Web Admin</h1>
      <p>企业管理后台：统一查看设备、运行器、渠道、审批、通知与治理状态。Gateway token 只进入 HTTP-only session cookie；浏览器业务请求仅访问 /api/omni/* server-side proxy，并携带 CSRF token。</p>
      <label>Gateway URL<input value={baseUrl} onChange={e => setBaseUrl(e.target.value)} /></label>
      <label>Session Token<input type="password" value={token} onChange={e => setToken(e.target.value)} autoComplete="off" /></label>
      <label>Actor<input value={actor} onChange={e => setActor(e.target.value)} /></label>
      <label>Role
        <select value={role} onChange={e => setRole(e.target.value as AdminRole)}>
          <option value="viewer">viewer</option>
          <option value="operator">operator</option>
          <option value="owner">owner</option>
        </select>
      </label>
      <p className="hint">{ROLE_HELP[role]}</p>
      <button onClick={establishSession}>建立 Web Session / CSRF</button>{' '}<button onClick={load}>连接并加载后台</button>
      {error && <p className="error">{error}</p>}
    </section>
    <section className="grid">
      <div className="card"><h2>设备</h2><pre>{JSON.stringify(snapshot?.devices || [], null, 2)}</pre></div>
      <div className="card"><h2>Runtime</h2><pre>{JSON.stringify(snapshot?.runtime_status || [], null, 2)}</pre></div>
      <div className="card"><h2>渠道生态</h2><pre>{JSON.stringify(ecosystem?.channels?.slice?.(0, 18) || [], null, 2)}</pre></div>
    </section>
    <section className="card">
      <h2>待审批</h2>
      <label>审批原因 / Audit Reason<textarea value={decisionReason} onChange={e => setDecisionReason(e.target.value)} /></label>
      {approvals.length === 0 && <p>暂无待审批动作。</p>}
      {approvals.map((approval: any) => <div key={approval.approval_id} className="card">
        <strong>{approval.action}</strong>
        <p>Risk: {approval.risk} / Requested by: {approval.requested_by} / Expires: {approval.expires_at ? new Date(approval.expires_at * 1000).toLocaleString() : 'n/a'}</p>
        <p>{approval.reason}</p>
        <button disabled={!canApprove} onClick={() => decide(approval.approval_id, 'approved')}>批准</button>{' '}
        <button disabled={!canApprove} onClick={() => decide(approval.approval_id, 'rejected')}>拒绝</button>
      </div>)}
    </section>
    <section className="card"><h2>通知</h2><pre>{JSON.stringify(snapshot?.notifications || [], null, 2)}</pre></section>
  </main>;
}
