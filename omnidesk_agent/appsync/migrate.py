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
    parser.add_argument(
        "--check",
        "--check-only",
        dest="check_only",
        action="store_true",
        help="Verify that the AppSync schema is current without applying migrations.",
    )
    args = parser.parse_args(argv)

    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"Missing PostgreSQL DSN environment variable: {args.dsn_env}"
        )

    if args.check_only:
        try:
            import psycopg  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for AppSync schema checks") from exc
        with psycopg.connect(dsn) as conn:
            status = appsync_schema_status(conn, namespace=args.namespace)
        print(json.dumps(status, sort_keys=True))
        return 0 if bool(status["ready"]) else 1

    applied = apply_appsync_migrations(dsn, namespace=args.namespace)
    versions = ",".join(str(version) for version in applied) or "none"
    print(f"applied AppSync migrations: {versions}")
    print(json.dumps({"namespace": args.namespace, "applied": applied}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
