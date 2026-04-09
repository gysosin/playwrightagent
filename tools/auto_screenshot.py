"""Auto-logging callback — logs every browser action to PostgreSQL and saves screenshots to MinIO.

Uses ADK's after_tool_callback. When browser_take_screenshot is called,
the callback extracts the base64 image and uploads it to MinIO.
For all browser actions, it creates a step_log entry in the database.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone

from google.adk.tools.base_tool import BaseTool

from db.connection import init_pool
from db.queries import create_step_log
from storage.minio_client import get_presigned_url, upload_screenshot

logger = logging.getLogger(__name__)

# Browser tools that represent meaningful actions to log.
_ACTION_TOOLS = {
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_hover",
    "browser_press_key",
    "browser_select_option",
    "browser_drag",
    "browser_file_upload",
    "browser_fill_form",
    "browser_navigate_back",
    "browser_handle_dialog",
    "browser_take_screenshot",
}

_pool_ready = False


async def _ensure_pool() -> None:
    global _pool_ready
    if not _pool_ready:
        try:
            await init_pool()
            _pool_ready = True
        except Exception:
            logger.debug("Could not init pool in auto_screenshot", exc_info=True)


def _extract_screenshot_b64(result) -> str | None:
    """Try to extract base64 PNG from a browser_take_screenshot result."""
    try:
        if isinstance(result, dict):
            content = result.get("content", [])
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image" and "data" in part:
                        return part["data"]
                    if part.get("type") == "text" and "base64" in part.get("text", "")[:50]:
                        return part["text"]
    except Exception:
        pass
    return None


async def after_browser_action(
    tool: BaseTool,
    args: dict,
    tool_context,
    tool_response,
) -> dict | None:
    """After-tool callback: log browser actions to DB + save screenshots to MinIO."""
    tool_name = tool.name if hasattr(tool, "name") else str(tool)

    if tool_name not in _ACTION_TOOLS:
        return None

    # Get session IDs from agent state.
    state = tool_context.state if hasattr(tool_context, "state") else {}
    task_id = state.get("_task_id")
    execution_id = state.get("_execution_id")

    if not task_id or not execution_id:
        return None

    await _ensure_pool()
    if not _pool_ready:
        return None

    # Increment step counter in state.
    step_count = state.get("_step_count", 0) + 1
    state["_step_count"] = step_count

    # Build action description.
    action_desc = tool_name
    if "url" in args:
        action_desc = f"navigate → {args['url']}"
    elif "element" in args:
        action_desc = f"{tool_name} → {args['element']}"
    elif "text" in args and tool_name == "browser_type":
        action_desc = f"type → {str(args['text'])[:50]}"
    elif "ref" in args:
        action_desc = f"{tool_name} → ref={args['ref']}"

    # Check success/failure.
    status = "success"
    error = None
    if isinstance(tool_response, dict) and tool_response.get("isError"):
        status = "failed"
        content = tool_response.get("content", [])
        if content and isinstance(content, list) and isinstance(content[0], dict):
            error = str(content[0].get("text", ""))[:500]

    # If this was a screenshot action, save the image to MinIO and strip base64 from response.
    snapshot_key = None
    presigned_url = None
    replaced_response = None

    if tool_name == "browser_take_screenshot":
        screenshot_b64 = _extract_screenshot_b64(tool_response)
        if screenshot_b64:
            try:
                png_bytes = base64.b64decode(screenshot_b64)
                timestamp = datetime.now(timezone.utc).isoformat()
                snapshot_key = await upload_screenshot(
                    task_id=task_id,
                    execution_id=execution_id,
                    step_index=step_count,
                    png_bytes=png_bytes,
                    timestamp=timestamp,
                )
                presigned_url = await asyncio.to_thread(get_presigned_url, snapshot_key)
                logger.info("Screenshot saved: step=%d key=%s", step_count, snapshot_key)
            except Exception:
                logger.debug("Failed to upload screenshot", exc_info=True)

        # Replace the huge base64 response with a tiny summary.
        # This prevents the base64 blob from eating up context tokens.
        replaced_response = {
            "screenshot_saved": True,
            "snapshot_key": snapshot_key,
            "presigned_url": presigned_url,
            "message": "Screenshot captured and saved to storage.",
        }

    # Save step log to DB.
    try:
        await create_step_log(
            execution_id=execution_id,
            step_index=step_count,
            action={
                "tool": tool_name,
                "args": {k: str(v)[:200] for k, v in args.items()},
                "description": action_desc,
            },
            status=status,
            snapshot_key=snapshot_key,
            error=error,
        )
    except Exception:
        logger.debug("Failed to log step %d", step_count, exc_info=True)

    # Return replaced response for screenshots (strips base64), None for other tools.
    return replaced_response
