"""Database query functions for all application tables.

Every function uses the helpers from :mod:`db.connection` and returns
plain ``dict`` objects so callers never need to handle asyncpg
:class:`~asyncpg.Record` instances directly.
"""

from __future__ import annotations

import json

from db.connection import execute, fetch, fetchrow


# ------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------

async def create_task(name: str, nl_steps: str) -> dict:
    """Insert a new task and return it as a dict."""
    row = await fetchrow(
        """
        INSERT INTO tasks (name, nl_steps)
        VALUES ($1, $2)
        RETURNING *
        """,
        name,
        nl_steps,
    )
    return dict(row)


async def get_task_by_name(name: str) -> dict | None:
    """Look up a task by its unique name."""
    row = await fetchrow("SELECT * FROM tasks WHERE name = $1", name)
    return dict(row) if row else None


async def get_task_by_id(task_id: str) -> dict | None:
    """Look up a task by its UUID."""
    row = await fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    return dict(row) if row else None


# ------------------------------------------------------------------
# Step sequences
# ------------------------------------------------------------------

async def create_step_sequence(
    task_id: str,
    revision: int,
    steps: list[dict],
) -> dict:
    """Insert a new step sequence with JSONB steps."""
    row = await fetchrow(
        """
        INSERT INTO step_sequences (task_id, revision, steps)
        VALUES ($1, $2, $3::jsonb)
        RETURNING *
        """,
        task_id,
        revision,
        json.dumps(steps),
    )
    return dict(row)


async def get_active_sequence(task_id: str) -> dict | None:
    """Return the currently active step sequence for a task."""
    row = await fetchrow(
        """
        SELECT * FROM step_sequences
        WHERE task_id = $1 AND is_active = true
        ORDER BY revision DESC
        LIMIT 1
        """,
        task_id,
    )
    return dict(row) if row else None


async def deactivate_all_sequences(task_id: str) -> None:
    """Mark every step sequence for a task as inactive."""
    await execute(
        "UPDATE step_sequences SET is_active = false WHERE task_id = $1",
        task_id,
    )


async def get_next_revision(task_id: str) -> int:
    """Return ``max(revision) + 1`` for the task, or ``1`` if none exist."""
    val = await fetchrow(
        "SELECT COALESCE(MAX(revision), 0) + 1 AS next_rev FROM step_sequences WHERE task_id = $1",
        task_id,
    )
    return val["next_rev"]


# ------------------------------------------------------------------
# Executions
# ------------------------------------------------------------------

async def create_execution(task_id: str, step_sequence_id: str | None = None) -> dict:
    """Create a new execution record with status ``running``."""
    row = await fetchrow(
        """
        INSERT INTO executions (task_id, step_sequence_id, status)
        VALUES ($1, $2, 'running')
        RETURNING *
        """,
        task_id,
        step_sequence_id,
    )
    return dict(row)


async def update_execution_status(
    execution_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update an execution's status and optionally record an error.

    When *status* is a terminal state (``completed`` or ``failed``),
    ``completed_at`` is set to ``now()``.
    """
    await execute(
        """
        UPDATE executions
        SET status = $2,
            error  = $3,
            completed_at = CASE WHEN $2 IN ('completed', 'failed') THEN now() ELSE completed_at END
        WHERE id = $1
        """,
        execution_id,
        status,
        error,
    )


# ------------------------------------------------------------------
# Step logs
# ------------------------------------------------------------------

async def create_step_log(
    execution_id: str,
    step_index: int,
    action: dict,
    status: str,
    snapshot_key: str | None = None,
    error: str | None = None,
) -> dict:
    """Record a single step execution log entry."""
    row = await fetchrow(
        """
        INSERT INTO step_logs (execution_id, step_index, action, status, snapshot_key, error)
        VALUES ($1, $2, $3::jsonb, $4, $5, $6)
        RETURNING *
        """,
        execution_id,
        step_index,
        json.dumps(action),
        status,
        snapshot_key,
        error,
    )
    return dict(row)


# ------------------------------------------------------------------
# Revisions
# ------------------------------------------------------------------

async def create_revision(
    task_id: str,
    old_sequence_id: str,
    new_sequence_id: str,
    failed_step: int,
    reason: str,
) -> dict:
    """Record a revision event linking old and new step sequences."""
    row = await fetchrow(
        """
        INSERT INTO revisions (task_id, old_sequence_id, new_sequence_id, failed_step, reason)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        task_id,
        old_sequence_id,
        new_sequence_id,
        failed_step,
        reason,
    )
    return dict(row)


# ------------------------------------------------------------------
# History helpers
# ------------------------------------------------------------------

async def get_execution_history(task_id: str, limit: int = 10) -> list[dict]:
    """Return recent executions for a task, newest first."""
    rows = await fetch(
        """
        SELECT * FROM executions
        WHERE task_id = $1
        ORDER BY started_at DESC
        LIMIT $2
        """,
        task_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_step_logs_for_execution(execution_id: str) -> list[dict]:
    """Return all step logs for an execution ordered by step index."""
    rows = await fetch(
        """
        SELECT * FROM step_logs
        WHERE execution_id = $1
        ORDER BY step_index
        """,
        execution_id,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# SOP Playbooks
# ------------------------------------------------------------------

async def get_sop_playbook(sop_id: str) -> dict | None:
    """Get a saved SOP playbook by its ID."""
    row = await fetchrow(
        "SELECT * FROM sop_playbooks WHERE sop_id = $1",
        sop_id,
    )
    return dict(row) if row else None


async def save_sop_playbook(sop_id: str, steps: list[dict]) -> dict:
    """Save or update an SOP playbook. Bumps version on update."""
    existing = await get_sop_playbook(sop_id)
    if existing:
        row = await fetchrow(
            """
            UPDATE sop_playbooks
            SET steps = $2::jsonb, version = version + 1, last_success_at = now()
            WHERE sop_id = $1
            RETURNING *
            """,
            sop_id,
            json.dumps(steps),
        )
    else:
        row = await fetchrow(
            """
            INSERT INTO sop_playbooks (sop_id, steps, version)
            VALUES ($1, $2::jsonb, 1)
            RETURNING *
            """,
            sop_id,
            json.dumps(steps),
        )
    return dict(row)
