from __future__ import annotations

import pytest

from omnidesk_agent.appsync import migrate


def test_migration_cli_applies_pending_versions(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TEST_APPSYNC_DSN", "postgresql://example/appsync")
    observed: dict[str, object] = {}

    def apply(dsn: str, *, namespace: str) -> list[int]:
        observed.update(dsn=dsn, namespace=namespace)
        return [1, 2, 3]

    monkeypatch.setattr(migrate, "apply_appsync_migrations", apply)

    assert migrate.main(
        [
            "--dsn-env",
            "TEST_APPSYNC_DSN",
            "--namespace",
            "tenant-a",
        ]
    ) == 0
    assert observed == {
        "dsn": "postgresql://example/appsync",
        "namespace": "tenant-a",
    }
    assert "applied AppSync migrations: 1,2,3" in capsys.readouterr().out


def test_migration_cli_check_only_reports_current_schema(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TEST_APPSYNC_DSN", "postgresql://example/appsync")

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Psycopg:
        @staticmethod
        def connect(dsn: str) -> Connection:
            assert dsn == "postgresql://example/appsync"
            return Connection()

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Psycopg())
    monkeypatch.setattr(
        migrate,
        "appsync_schema_status",
        lambda _conn, *, namespace: {
            "current": 3,
            "required": 3,
            "ready": namespace == "tenant-b",
        },
    )

    assert migrate.main(
        [
            "--dsn-env",
            "TEST_APPSYNC_DSN",
            "--namespace",
            "tenant-b",
            "--check-only",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert '"current": 3' in output
    assert '"ready": true' in output


def test_migration_cli_fails_closed_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_APPSYNC_DSN", raising=False)
    with pytest.raises(RuntimeError, match="Missing PostgreSQL DSN"):
        migrate.main(["--dsn-env", "MISSING_APPSYNC_DSN"])
