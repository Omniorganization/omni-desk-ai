#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def require(path: Path, *needles: str) -> list[str]:
    text = path.read_text(encoding='utf-8')
    return [f'{path}: missing {needle}' for needle in needles if needle not in text]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    issues: list[str] = []
    issues += require(
        root / 'omnidesk_agent/repositories/postgres_pool.py',
        'class SharedPostgresConnectionPool',
        'def connection(',
        'def ping(',
        "'waiters': self._waiters",
    )
    issues += require(
        root / 'omnidesk_agent/repositories/postgres.py',
        'def _connection_pool(',
        'def readiness_check(',
        'def pool_stats(',
        'def close(',
    )
    issues += require(
        root / 'omnidesk_agent/repositories/postgres_state.py',
        'idx_omnidesk_runs_waiting_approval',
        'idx_omnidesk_jobs_ready',
        'def claim_ready_by_status(',
        'return self.state.find_by_field(self.namespace, "waiting_approval_id", approval_id)',
    )
    issues += require(
        root / 'omnidesk_agent/server.py',
        'await asyncio.to_thread(_readiness_snapshot_sync, deep=deep)',
        'readiness_cache',
        'await _readiness_snapshot(deep=True)',
    )
    issues += require(
        root / 'omnidesk_agent/daemon.py',
        '"whatsapp": adapters["whatsapp_cloud"]',
        '"repository_pool":',
        'getattr(self, "repository_factory", None)',
    )
    if issues:
        for issue in issues:
            print(f'BLOCKER {issue}', file=sys.stderr)
        return 1
    print('industrial runtime optimization contract passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
