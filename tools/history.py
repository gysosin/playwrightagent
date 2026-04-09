"""Retrieve execution history for a task.

Provides :func:`get_history`, an ADK tool that loads past executions and
their step logs from the database, enriching each snapshot with a presigned
MinIO URL for convenient viewing.
"""

from __future__ import annotations

import asyncio
import logging

from db.queries import (
    get_execution_history,
    get_step_logs_for_execution,
    get_task_by_name,
)
from storage.minio_client import get_presigned_url

logger = logging.getLogger(__name__)


def _serialise_record(record: dict) -> dict:
    """Convert non-JSON-serialisable values (UUIDs, datetimes) to strings."""
    out: dict = {}
    for key, value in record.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = str(value) if not isinstance(value, (str, int, float, bool, list, dict, type(None))) else value
    return out


async def get_history(task_name: str, limit: int = 10) -> dict:
    """Get execution history for a task.

    For each execution, all step logs are included.  Where a step log has a
    ``snapshot_key``, a presigned MinIO URL is attached for easy access.

    Args:
        task_name: The unique name of the task to query.
        limit: Maximum number of executions to return (newest first).

    Returns:
        A dict with keys: task_id, task_name, executions.
        Each execution includes its step_logs with snapshot_key and presigned_url.
    """
    task = await get_task_by_name(task_name)
    if task is None:
        return {"error": f"Task {task_name!r} not found."}

    task_id = str(task["id"])
    executions_raw = await get_execution_history(task_id, limit)

    executions: list[dict] = []
    for exec_record in executions_raw:
        exec_dict = _serialise_record(exec_record)
        execution_id = str(exec_record["id"])

        logs_raw = await get_step_logs_for_execution(execution_id)
        enriched_logs: list[dict] = []
        for log in logs_raw:
            log_dict = _serialise_record(log)
            snapshot_key = log.get("snapshot_key")
            if snapshot_key:
                try:
                    presigned = await asyncio.to_thread(
                        get_presigned_url, snapshot_key,
                    )
                    log_dict["presigned_url"] = presigned
                except Exception:
                    logger.warning(
                        "Could not generate presigned URL for %s",
                        snapshot_key,
                    )
                    log_dict["presigned_url"] = None
            else:
                log_dict["presigned_url"] = None

            enriched_logs.append(log_dict)

        exec_dict["step_logs"] = enriched_logs
        executions.append(exec_dict)

    logger.info(
        "Returning %d executions for task %s",
        len(executions),
        task_name,
    )

    return {
        "task_id": task_id,
        "task_name": task_name,
        "executions": executions,
    }
