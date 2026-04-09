"""Upload a screenshot to MinIO and log it in the database.

This module exposes :func:`save_snapshot`, an ADK tool that captures the
upload-to-object-storage and DB-logging in a single call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from storage.minio_client import get_presigned_url, upload_screenshot

logger = logging.getLogger(__name__)


async def save_snapshot(
    task_id: str,
    execution_id: str,
    step_index: int,
    png_bytes: bytes,
) -> dict:
    """Upload a screenshot to MinIO and return storage metadata.

    Args:
        task_id: UUID of the automation task.
        execution_id: UUID of the current execution run.
        step_index: Zero-based index of the step within the execution.
        png_bytes: Raw PNG image bytes.

    Returns:
        A dict with keys: snapshot_key, presigned_url.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    snapshot_key = await upload_screenshot(
        task_id=task_id,
        execution_id=execution_id,
        step_index=step_index,
        png_bytes=png_bytes,
        timestamp=timestamp,
    )

    # get_presigned_url is synchronous — run in a thread to avoid blocking.
    presigned_url = await asyncio.to_thread(get_presigned_url, snapshot_key)

    logger.info(
        "Snapshot saved: task=%s exec=%s step=%d key=%s",
        task_id,
        execution_id,
        step_index,
        snapshot_key,
    )

    return {
        "snapshot_key": snapshot_key,
        "presigned_url": presigned_url,
    }
