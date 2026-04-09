"""Database bootstrap script.

Creates the ``adk_automation`` database (if it does not exist) on the
configured PostgreSQL server and then applies all table DDL.

Run directly::

    python db/migrations.py
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import quote_plus

import asyncpg

from config import get_settings

# ------------------------------------------------------------------
# DDL statements
# ------------------------------------------------------------------

_DDL = """\
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    nl_steps TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS step_sequences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    revision INT NOT NULL,
    steps JSONB NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    step_sequence_id UUID REFERENCES step_sequences(id),
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error TEXT
);

CREATE TABLE IF NOT EXISTS step_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID REFERENCES executions(id) ON DELETE CASCADE,
    step_index INT NOT NULL,
    action JSONB NOT NULL,
    status TEXT NOT NULL,
    snapshot_key TEXT,
    executed_at TIMESTAMPTZ DEFAULT now(),
    error TEXT
);

CREATE TABLE IF NOT EXISTS revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    old_sequence_id UUID REFERENCES step_sequences(id),
    new_sequence_id UUID REFERENCES step_sequences(id),
    failed_step INT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


def _admin_dsn() -> str:
    """Build a DSN that connects to the ``maindb`` database."""
    s = get_settings()
    return (
        f"postgresql://{quote_plus(s.POSTGRES_USER)}"
        f":{quote_plus(s.POSTGRES_PASSWORD.get_secret_value())}"
        f"@{s.POSTGRES_HOST}:{s.POSTGRES_PORT}/maindb"
    )


async def _ensure_database() -> None:
    """Create the ``adk_automation`` database if it does not already exist."""
    conn: asyncpg.Connection = await asyncpg.connect(_admin_dsn())
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            "adk_automation",
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block.
            await conn.execute("CREATE DATABASE adk_automation")
            print("Created database: adk_automation")
        else:
            print("Database adk_automation already exists")
    finally:
        await conn.close()


async def _apply_ddl() -> None:
    """Connect to ``adk_automation`` and run all table DDL."""
    conn: asyncpg.Connection = await asyncpg.connect(
        get_settings().postgres_dsn,
    )
    try:
        await conn.execute(_DDL)
        print("All tables created / verified")
    finally:
        await conn.close()


async def run_migrations() -> None:
    """Public entry point: create the database then apply DDL."""
    await _ensure_database()
    await _apply_ddl()


def main() -> None:
    """CLI entry point for ``python db/migrations.py``."""
    try:
        asyncio.run(run_migrations())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
