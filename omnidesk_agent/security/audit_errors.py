from __future__ import annotations


class AuditIntegrityError(PermissionError):
    """The audit log cannot be extended or trusted; the audited action must not run."""
