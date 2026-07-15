from pathlib import Path

path = Path("scripts/_apply_industrial_l4_remediation.py")
text = path.read_text(encoding="utf-8")
old = '''replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                    None if terminal else reservation.lease_owner,\\n                    None if terminal else now + self.lease_seconds,\\n                    now,\\n                    reservation.namespace,\\n',
    '                    None if terminal else reservation.lease_owner,\\n                    terminal,\\n                    self.lease_seconds,\\n                    reservation.namespace,\\n',
)
'''
new = '''replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                    status,\\n                    sequence,\\n                    None if terminal else reservation.lease_owner,\\n                    None if terminal else now + self.lease_seconds,\\n                    now,\\n                    reservation.namespace,\\n',
    '                    status,\\n                    sequence,\\n                    None if terminal else reservation.lease_owner,\\n                    terminal,\\n                    self.lease_seconds,\\n                    reservation.namespace,\\n',
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one ambiguous lease replacement, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

migration_path = Path("omnidesk_agent/appsync/postgres_migrations.py")
migration_text = migration_path.read_text(encoding="utf-8")
migration_text = migration_text.replace("from contextlib import AbstractContextManager\n", "")
old_cursor = "    def cursor(self) -> AbstractContextManager[_Cursor]: ...\n"
new_cursor = "    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...\n"
if migration_text.count(old_cursor) != 1:
    raise RuntimeError(f"expected one migration cursor protocol, found {migration_text.count(old_cursor)}")
migration_path.write_text(migration_text.replace(old_cursor, new_cursor, 1), encoding="utf-8")
print("L4 remediation driver and psycopg protocol disambiguated")
