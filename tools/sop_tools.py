"""SOP playbook tools — record successful flows and replay them.

Flow:
1. Agent receives an SOP task
2. Call load_sop_playbook to check if we have a saved flow
3. If yes → agent follows the saved steps (snapshot → find element → act)
4. If no → agent does smart mode, then calls save_sop_playbook to record the flow
5. If replay fails → agent switches to smart mode, saves updated playbook
"""

from __future__ import annotations

import json
import logging

from db.connection import init_pool
from db.queries import (
    get_sop_playbook,
    save_sop_playbook as _db_save_sop,
    get_step_logs_for_execution,
)

logger = logging.getLogger(__name__)

_pool_ready = False


async def _ensure_pool() -> None:
    global _pool_ready
    if not _pool_ready:
        await init_pool()
        _pool_ready = True


async def load_sop_playbook(sop_id: str) -> dict:
    """Load a saved SOP playbook.

    If a playbook exists for this SOP ID, returns the recorded steps.
    The agent should follow these steps using snapshot → find element → act.
    If no playbook exists, the agent should proceed in smart mode.

    Args:
        sop_id: The SOP identifier (e.g. "SOP-8", "sop_nuskin_cart").

    Returns:
        Dict with playbook steps if found, or a message to proceed in smart mode.
    """
    await _ensure_pool()
    playbook = await get_sop_playbook(sop_id)

    if playbook is None:
        return {
            "found": False,
            "sop_id": sop_id,
            "message": "No saved playbook. Proceed in smart mode — snapshot, find elements, act. After success, call save_sop_playbook to record the flow.",
        }

    steps = playbook["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps)

    return {
        "found": True,
        "sop_id": sop_id,
        "version": playbook["version"],
        "last_success": str(playbook.get("last_success_at", "")),
        "steps": steps,
        "message": (
            f"Playbook found (v{playbook['version']}). Follow these steps in order. "
            "For each step: take a browser_snapshot, find the matching element, execute the action. "
            "If a step fails because the site changed, adapt using the snapshot and continue. "
            "After completing all steps, call save_sop_playbook with the updated steps."
        ),
    }


async def save_sop_playbook(sop_id: str, steps: list[dict]) -> dict:
    """Save or update an SOP playbook after a successful run.

    Call this after completing an SOP successfully. Pass the list of
    semantic steps that were executed (what was done, not the raw ref numbers).

    Each step should be a dict like:
        {"action": "navigate", "url": "https://...", "description": "Go to homepage"}
        {"action": "click", "target": "Accept all cookies button", "description": "Dismiss cookie banner"}
        {"action": "type", "target": "search input", "text": "ageloc", "description": "Search for product"}
        {"action": "click", "target": "Select Options on first product", "description": "Open product options"}
        {"action": "click", "target": "Add to cart button", "description": "Add item to cart"}
        {"action": "read", "target": "cart total", "description": "Read the total amount"}

    Use human-readable target descriptions (NOT ref numbers, since those change).

    Args:
        sop_id: The SOP identifier.
        steps: List of semantic step dicts describing the successful flow.

    Returns:
        Confirmation with the saved version number.
    """
    await _ensure_pool()
    result = await _db_save_sop(sop_id, steps)

    logger.info("SOP playbook saved: %s v%d (%d steps)", sop_id, result["version"], len(steps))

    return {
        "sop_id": sop_id,
        "version": result["version"],
        "steps_saved": len(steps),
        "message": f"Playbook saved (v{result['version']}). Next time this SOP runs, these steps will be replayed.",
    }


async def record_sop_from_execution(sop_id: str, execution_id: str) -> dict:
    """Auto-record an SOP playbook from a completed execution's step logs.

    Reads the step_logs for an execution, extracts the successful actions,
    and saves them as a playbook. Call this after a successful execution.

    Args:
        sop_id: The SOP identifier.
        execution_id: The execution_id from the completed session.

    Returns:
        Confirmation with the saved playbook.
    """
    await _ensure_pool()
    logs = await get_step_logs_for_execution(execution_id)

    # Extract only successful actions (skip snapshots/screenshots).
    steps = []
    for log in logs:
        if log["status"] != "success":
            continue
        action = log["action"]
        if isinstance(action, str):
            action = json.loads(action)
        tool_name = action.get("tool", "")
        if tool_name in ("browser_snapshot", "browser_take_screenshot"):
            continue

        step = {
            "action": tool_name.replace("browser_", ""),
            "description": action.get("description", ""),
            "args": action.get("args", {}),
        }
        steps.append(step)

    if not steps:
        return {"error": "No successful steps found in execution."}

    result = await _db_save_sop(sop_id, steps)
    logger.info("SOP auto-recorded: %s v%d (%d steps)", sop_id, result["version"], len(steps))

    return {
        "sop_id": sop_id,
        "version": result["version"],
        "steps_recorded": len(steps),
        "steps": steps,
    }
