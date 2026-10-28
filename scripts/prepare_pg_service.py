#!/usr/bin/env python3
"""Create a mode-0600 libpq service file from DATABASE_URL without echoing secrets."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ALLOWED_QUERY_KEYS = {"sslmode", "application_name"}
VALID_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def quote_service_value(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Database URL fields cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--service-name", default="patentshield_backup")
    args = parser.parse_args()
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        print("DATABASE_URL must be a valid postgres/postgresql URL", file=sys.stderr)
        return 2
    dbname = unquote(parsed.path.lstrip("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not dbname or not username or not password:
        print("DATABASE_URL must include database, username and password", file=sys.stderr)
        return 2
    query = parse_qs(parsed.query, keep_blank_values=True)
    unknown = sorted(set(query) - ALLOWED_QUERY_KEYS)
    if unknown:
        print("Unsupported DATABASE_URL query option(s): " + ", ".join(unknown), file=sys.stderr)
        return 2
    sslmode = query.get("sslmode", ["prefer"])[0]
    if sslmode not in VALID_SSLMODES:
        print("Unsupported sslmode value", file=sys.stderr)
        return 2
    app_name = query.get("application_name", ["patentshield-backup"])[0]
    values = {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "dbname": dbname,
        "user": username,
        "password": password,
        "sslmode": sslmode,
        "application_name": app_name,
    }
    try:
        content = "[" + args.service_name + "]\n" + "".join(
            f"{key}={quote_service_value(value)}\n" for key, value in values.items()
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
        os.chmod(args.output, 0o600)
    except (OSError, ValueError) as exc:
        print(f"Could not prepare temporary PostgreSQL service file: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote temporary libpq service config to {args.output}; secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
