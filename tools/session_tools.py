"""ADK tools for session logging and screenshot storage.

These tools are designed to work alongside the Playwright MCP tools in
Approach C (agent-driven). The agent calls these to persist execution
history and screenshots to PostgreSQL + MinIO.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone

from db.connection import init_pool
from db.queries import (
    create_execution,
    create_step_log,
    create_task,
    get_execution_history,
    get_step_logs_for_execution,
    get_task_by_name,
    update_execution_status,
)
from storage.minio_client import get_presigned_url, upload_screenshot

logger = logging.getLogger(__name__)

_pool_ready = False


async def _ensure_pool() -> None:
    global _pool_ready
    if not _pool_ready:
        await init_pool()
        _pool_ready = True


async def start_session(task_name: str, description: str = "", tool_context=None) -> dict:
    """Start a new automation session. Call this at the beginning of a task.

    Stores task_id and execution_id in agent state so the auto-screenshot
    callback can access them without the agent passing IDs manually.

    Args:
        task_name: Short name for this automation task.
        description: Optional description of what the task does.

    Returns:
        Dict with task_id and execution_id.
    """
    await _ensure_pool()

    task = await get_task_by_name(task_name)
    if task is None:
        task = await create_task(task_name, description)

    task_id = str(task["id"])
    execution = await create_execution(task_id, step_sequence_id=None)
    execution_id = str(execution["id"])

    # Store in agent state for the auto-screenshot callback.
    if tool_context and hasattr(tool_context, "state"):
        tool_context.state["_task_id"] = task_id
        tool_context.state["_execution_id"] = execution_id
        tool_context.state["_step_count"] = 0

    logger.info("Started session: task=%s execution=%s", task_id, execution_id)

    return {
        "task_id": task_id,
        "execution_id": execution_id,
        "message": f"Session started. Screenshots and logs will be saved automatically after every browser action.",
    }


async def log_step(
    execution_id: str,
    step_index: int,
    action_description: str,
    status: str = "success",
    error: str | None = None,
    screenshot_base64: str | None = None,
    task_id: str | None = None,
) -> dict:
    """Log a completed step with optional screenshot.

    Call this after each significant action to persist the execution trail.

    Args:
        execution_id: The execution_id from start_session.
        step_index: Zero-based step number.
        action_description: What was done in this step.
        status: "success", "failed", or "healed".
        error: Error message if the step failed.
        screenshot_base64: Base64-encoded PNG screenshot (from browser_take_screenshot).
        task_id: Task ID (needed if saving screenshot).

    Returns:
        Dict with step log details and optional screenshot URL.
    """
    await _ensure_pool()

    snapshot_key = None
    presigned_url = None

    if screenshot_base64 and task_id:
        try:
            png_bytes = base64.b64decode(screenshot_base64)
            timestamp = datetime.now(timezone.utc).isoformat()
            snapshot_key = await upload_screenshot(
                task_id=task_id,
                execution_id=execution_id,
                step_index=step_index,
                png_bytes=png_bytes,
                timestamp=timestamp,
            )
            presigned_url = await asyncio.to_thread(get_presigned_url, snapshot_key)
        except Exception:
            logger.warning("Failed to save screenshot for step %d", step_index, exc_info=True)

    await create_step_log(
        execution_id=execution_id,
        step_index=step_index,
        action={"description": action_description},
        status=status,
        snapshot_key=snapshot_key,
        error=error,
    )

    logger.info("Logged step %d: %s (%s)", step_index, action_description, status)

    result = {
        "step_index": step_index,
        "status": status,
        "action": action_description,
    }
    if snapshot_key:
        result["snapshot_key"] = snapshot_key
        result["presigned_url"] = presigned_url
    if error:
        result["error"] = error
    return result


async def end_session(execution_id: str, status: str = "completed", summary: str = "") -> dict:
    """End an automation session.

    Args:
        execution_id: The execution_id from start_session.
        status: Final status — "completed", "failed", or "healed".
        summary: Brief summary of what happened.

    Returns:
        Confirmation dict.
    """
    await _ensure_pool()
    await update_execution_status(execution_id, status)
    logger.info("Ended session %s with status: %s", execution_id, status)

    return {
        "execution_id": execution_id,
        "status": status,
        "summary": summary,
    }


async def get_session_history(task_name: str, limit: int = 5) -> dict:
    """Get past execution history for a task.

    Args:
        task_name: The task name to look up.
        limit: Max number of past executions to return.

    Returns:
        Dict with task info and list of past executions with their step logs.
    """
    await _ensure_pool()

    task = await get_task_by_name(task_name)
    if task is None:
        return {"error": f"Task '{task_name}' not found."}

    task_id = str(task["id"])
    executions_raw = await get_execution_history(task_id, limit)

    executions = []
    for rec in executions_raw:
        exec_dict = {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if not isinstance(v, (str, int, float, bool, list, dict, type(None))) else v) for k, v in rec.items()}
        logs = await get_step_logs_for_execution(str(rec["id"]))
        exec_dict["step_logs"] = [
            {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if not isinstance(v, (str, int, float, bool, list, dict, type(None))) else v) for k, v in log.items()}
            for log in logs
        ]
        executions.append(exec_dict)

    return {"task_id": task_id, "task_name": task_name, "executions": executions}
