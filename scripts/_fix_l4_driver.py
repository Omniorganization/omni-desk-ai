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

state_path = Path("omnidesk_agent/repositories/postgres_state.py")
state_text = state_path.read_text(encoding="utf-8")
old_query = '''                if column:
                    # Column name is selected from allowed_columns, never raw caller input.
                    cur.execute(
                        f"""
                        SELECT COALESCE({column}, ''), COUNT(*), COALESCE(SUM(input_tokens),0),
                               COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0)
                        FROM omnidesk_model_cost_events
                        WHERE created_at >= %s
                        GROUP BY COALESCE({column}, '')
                        """,  # nosec B608
                        (since,),
                    )
'''
new_query = '''                if column:
                    from psycopg import sql

                    column_identifier = sql.Identifier(column)
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT COALESCE({column}, ''), COUNT(*), COALESCE(SUM(input_tokens),0),
                                   COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0)
                            FROM omnidesk_model_cost_events
                            WHERE created_at >= %s
                            GROUP BY COALESCE({column}, '')
                            """
                        ).format(column=column_identifier),
                        (since,),
                    )
'''
if state_text.count(old_query) != 1:
    raise RuntimeError(f"expected one dynamic summary query, found {state_text.count(old_query)}")
state_path.write_text(state_text.replace(old_query, new_query, 1), encoding="utf-8")

guard_path = Path("omnidesk_agent/security/resource_guard.py")
guard_text = guard_path.read_text(encoding="utf-8")
old_allow = '''                row = cur.fetchone()
                return int(row[0]) <= limit
'''
new_allow = '''                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("PostgreSQL rate limiter did not return a count")
                return int(row[0]) <= limit
'''
if guard_text.count(old_allow) != 1:
    raise RuntimeError(f"expected one rate limiter count read, found {guard_text.count(old_allow)}")
guard_text = guard_text.replace(old_allow, new_allow, 1)
old_size = '''                row = cur.fetchone()
                return int(row[0])
'''
new_size = '''                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("PostgreSQL rate limiter size query returned no row")
                return int(row[0])
'''
if guard_text.count(old_size) != 1:
    raise RuntimeError(f"expected one rate limiter size read, found {guard_text.count(old_size)}")
guard_path.write_text(guard_text.replace(old_size, new_size, 1), encoding="utf-8")

print("L4 remediation driver and enterprise psycopg typing gaps fixed")
