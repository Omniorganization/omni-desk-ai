from __future__ import annotations

import argparse
import json
import os

from omnidesk_agent.appsync.postgres_migrations import (
    apply_appsync_migrations,
    appsync_schema_status,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply or verify versioned OmniDesk AppSync PostgreSQL migrations."
    )
    parser.add_argument(
        "--dsn-env",
        default="OMNIDESK_APPSYNC_POSTGRES_DSN",
        help="Environment variable containing the PostgreSQL DSN.",
    )
    parser.add_argument("--namespace", default="production")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"missing PostgreSQL DSN environment variable: {args.dsn_env}")

    if args.check:
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise SystemExit("psycopg is required") from exc
        with psycopg.connect(dsn) as conn:
            status = appsync_schema_status(conn, namespace=args.namespace)
        print(json.dumps(status, sort_keys=True))
        return 0 if bool(status["ready"]) else 1

    applied = apply_appsync_migrations(dsn, namespace=args.namespace)
    print(json.dumps({"namespace": args.namespace, "applied": applied}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
