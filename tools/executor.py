"""Execute the active step sequence for a task via the Playwright MCP server.

For every step the executor:
1. Dispatches the action to the MCP client.
2. Takes a screenshot.
3. Uploads it to MinIO (via :func:`tools.snapshot.save_snapshot`).
4. Logs the result in ``step_logs``.
5. On failure, invokes the healer and retries the step once.
"""

from __future__ import annotations

import json
import logging

from db.queries import (
    create_execution,
    create_step_log,
    get_active_sequence,
    get_task_by_name,
    update_execution_status,
)
from mcp_client.playwright_client import PlaywrightMCPClient
from tools.healer import heal_step
from tools.snapshot import save_snapshot

logger = logging.getLogger(__name__)

# Maximum number of heal attempts per step to prevent infinite loops.
_MAX_HEAL_ATTEMPTS = 1


async def _dispatch_action(
    client: PlaywrightMCPClient,
    action: dict,
) -> str | None:
    """Execute a single Playwright action and return an optional text result.

    Raises on failure so the caller can handle healing.
    """
    action_type = action.get("action", "")

    match action_type:
        case "navigate":
            await client.navigate(action["url"])
        case "click":
            await client.click(action["selector"])
        case "fill":
            await client.fill(action["selector"], action["value"])
        case "wait_for":
            timeout = action.get("timeout_ms", 5000)
            await client.wait_for(action["selector"], timeout_ms=timeout)
        case "screenshot":
            # The executor already takes a screenshot after every step, but
            # if the user explicitly requests one, we honor it.
            await client.screenshot()
        case "get_text":
            text = await client.get_text(action["selector"])
            return text
        case "close":
            await client.close_browser()
        case _:
            raise ValueError(f"Unknown action type: {action_type!r}")

    return None


async def execute_steps(task_name: str) -> dict:
    """Execute the active step sequence for a task.

    Loads the active step_sequence from DB, creates an execution record, and
    runs each step through the Playwright MCP server.  A screenshot is taken
    and uploaded after every step.  On failure the healer is invoked and the
    step is retried once.

    Args:
        task_name: The unique name of the task to execute.

    Returns:
        A dict with keys: execution_id, status, steps_executed, step_logs.
    """
    # --- Resolve task and active sequence ---------------------------------
    task = await get_task_by_name(task_name)
    if task is None:
        return {"error": f"Task {task_name!r} not found. Run interpret_steps first."}

    task_id = str(task["id"])
    sequence = await get_active_sequence(task_id)
    if sequence is None:
        return {"error": f"No active step sequence for task {task_name!r}."}

    sequence_id = str(sequence["id"])
    steps: list[dict] = sequence["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps)

    # --- Create execution record ------------------------------------------
    execution = await create_execution(task_id, sequence_id)
    execution_id = str(execution["id"])

    logger.info(
        "Starting execution %s for task %s (%d steps)",
        execution_id,
        task_name,
        len(steps),
    )

    step_logs: list[dict] = []
    final_status = "completed"
    healed = False

    async with PlaywrightMCPClient() as client:
        step_idx = 0
        while step_idx < len(steps):
            step = steps[step_idx]
            try:
                # Execute the action.
                result_text = await _dispatch_action(client, step)

                # Take a screenshot after every step.
                png_bytes = await client.screenshot()
                snap = await save_snapshot(
                    task_id=task_id,
                    execution_id=execution_id,
                    step_index=step_idx,
                    png_bytes=png_bytes,
                )

                await create_step_log(
                    execution_id=execution_id,
                    step_index=step_idx,
                    action=step,
                    status="success",
                    snapshot_key=snap["snapshot_key"],
                )
                log_entry = {
                    "step_index": step_idx,
                    "action": step,
                    "status": "success",
                    "snapshot_key": snap["snapshot_key"],
                    "presigned_url": snap["presigned_url"],
                }
                if result_text is not None:
                    log_entry["result_text"] = result_text

                step_logs.append(log_entry)
                logger.info("Step %d succeeded: %s", step_idx, step.get("description", ""))
                step_idx += 1

            except Exception as exc:
                error_msg = str(exc)
                logger.warning(
                    "Step %d failed: %s — attempting heal",
                    step_idx,
                    error_msg[:200],
                )

                # Attempt to capture a screenshot for healing context.
                try:
                    heal_screenshot = await client.screenshot()
                except Exception:
                    logger.warning("Could not capture screenshot for healing")
                    heal_screenshot = b""

                # Log the failure.
                snap_info: dict = {}
                if heal_screenshot:
                    try:
                        snap_info = await save_snapshot(
                            task_id=task_id,
                            execution_id=execution_id,
                            step_index=step_idx,
                            png_bytes=heal_screenshot,
                        )
                    except Exception:
                        logger.warning("Could not save failure screenshot", exc_info=True)

                await create_step_log(
                    execution_id=execution_id,
                    step_index=step_idx,
                    action=step,
                    status="failed",
                    snapshot_key=snap_info.get("snapshot_key"),
                    error=error_msg,
                )

                step_logs.append({
                    "step_index": step_idx,
                    "action": step,
                    "status": "failed",
                    "error": error_msg,
                    "snapshot_key": snap_info.get("snapshot_key"),
                    "presigned_url": snap_info.get("presigned_url"),
                })

                # --- Attempt healing ------------------------------------------
                if heal_screenshot:
                    try:
                        heal_result = await heal_step(
                            task_id=task_id,
                            execution_id=execution_id,
                            sequence_id=sequence_id,
                            steps=steps,
                            failed_step_index=step_idx,
                            failed_step=step,
                            error_message=error_msg,
                            current_page_screenshot=heal_screenshot,
                        )

                        # Update our working copies.
                        steps = heal_result["new_steps"]
                        sequence_id = heal_result["new_sequence_id"]
                        healed = True

                        logger.info(
                            "Healed step %d — retrying with new action",
                            step_idx,
                        )
                        # Retry the same step_idx with the healed action.
                        # (The while loop will re-execute this index.)
                        # But we only retry once; if it fails again we bail.
                        try:
                            result_text = await _dispatch_action(client, steps[step_idx])

                            png_bytes = await client.screenshot()
                            snap = await save_snapshot(
                                task_id=task_id,
                                execution_id=execution_id,
                                step_index=step_idx,
                                png_bytes=png_bytes,
                            )

                            await create_step_log(
                                execution_id=execution_id,
                                step_index=step_idx,
                                action=steps[step_idx],
                                status="healed",
                                snapshot_key=snap["snapshot_key"],
                            )

                            log_entry = {
                                "step_index": step_idx,
                                "action": steps[step_idx],
                                "status": "healed",
                                "snapshot_key": snap["snapshot_key"],
                                "presigned_url": snap["presigned_url"],
                            }
                            if result_text is not None:
                                log_entry["result_text"] = result_text
                            step_logs.append(log_entry)

                            logger.info("Healed step %d succeeded on retry", step_idx)
                            step_idx += 1
                            continue

                        except Exception as retry_exc:
                            logger.error(
                                "Healed step %d failed again: %s",
                                step_idx,
                                str(retry_exc)[:200],
                            )
                            await create_step_log(
                                execution_id=execution_id,
                                step_index=step_idx,
                                action=steps[step_idx],
                                status="failed",
                                error=str(retry_exc),
                            )
                            step_logs.append({
                                "step_index": step_idx,
                                "action": steps[step_idx],
                                "status": "failed",
                                "error": str(retry_exc),
                            })
                            final_status = "failed"
                            break

                    except Exception as heal_exc:
                        logger.error(
                            "Healing itself failed: %s",
                            str(heal_exc)[:200],
                        )
                        final_status = "failed"
                        break
                else:
                    # No screenshot available for healing — cannot heal.
                    final_status = "failed"
                    break

    # --- Finalize execution ------------------------------------------------
    if healed and final_status == "completed":
        final_status = "healed"

    await update_execution_status(execution_id, final_status)

    logger.info(
        "Execution %s finished: status=%s steps_executed=%d",
        execution_id,
        final_status,
        len(step_logs),
    )

    return {
        "execution_id": execution_id,
        "status": final_status,
        "steps_executed": len(step_logs),
        "step_logs": step_logs,
    }
