export type ControlHubStatus = 'passed' | 'blocked' | 'pending' | 'unknown';

export interface ControlHubPanel {
  id: string;
  title: string;
  status: ControlHubStatus;
  count: number;
}

export function buildControlHubPanels(snapshot: any, ecosystem: any): ControlHubPanel[] {
  const pendingApprovals = snapshot?.pending_approvals || [];
  const runtime = snapshot?.runtime_status || [];
  const notifications = snapshot?.notifications || [];
  const channels = ecosystem?.channels || [];
  const externalEvidence = snapshot?.external_evidence || ecosystem?.external_evidence || {};
  return [
    {
      id: 'approvals',
      title: 'Approvals',
      status: pendingApprovals.length ? 'blocked' : 'passed',
      count: pendingApprovals.length,
    },
    {
      id: 'runtime',
      title: 'Runtime',
      status: runtime.some((item: any) => item?.status === 'degraded' || item?.status === 'offline') ? 'blocked' : 'passed',
      count: runtime.length,
    },
    {
      id: 'channels',
      title: 'Channels',
      status: channels.length ? 'pending' : 'unknown',
      count: channels.length,
    },
    {
      id: 'push',
      title: 'Push',
      status: notifications.length ? 'pending' : 'unknown',
      count: notifications.length,
    },
    {
      id: 'evidence',
      title: 'External Evidence',
      status: externalEvidence.status === 'passed' ? 'passed' : 'blocked',
      count: externalEvidence.required_categories?.length || 7,
    },
  ];
}
