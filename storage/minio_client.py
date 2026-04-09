"""MinIO object-storage helpers for screenshot uploads.

Usage::

    from storage.minio_client import upload_screenshot, get_presigned_url

    # During an automation step
    object_key = await upload_screenshot(
        task_id="abc-123",
        execution_id="exec-456",
        step_index=1,
        png_bytes=raw_png,
        timestamp="2025-01-15T10:30:00",
    )

    # Later, when serving the image to a user
    url = get_presigned_url(object_key)
"""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from config import get_settings

_client: Minio | None = None


def get_client() -> Minio:
    """Return a configured MinIO client (lazy singleton).

    The client is created on the first call and reused afterwards.
    It connects over plain HTTP (``secure=False``) because this is
    local infrastructure.
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    _client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
        secure=False,
    )
    return _client


def _ensure_bucket_sync() -> None:
    """Create the bucket if it does not already exist (synchronous)."""
    client = get_client()
    bucket = get_settings().MINIO_BUCKET
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


async def ensure_bucket() -> None:
    """Create the bucket if it does not already exist.

    Wraps the synchronous MinIO SDK call in a thread executor so it
    can be safely ``await``-ed from async code without blocking the
    event loop.
    """
    await asyncio.to_thread(_ensure_bucket_sync)


def _upload_screenshot_sync(
    task_id: str,
    execution_id: str,
    step_index: int,
    png_bytes: bytes,
    timestamp: str,
) -> str:
    """Upload PNG bytes to MinIO (synchronous). Returns the object key."""
    client = get_client()
    bucket = get_settings().MINIO_BUCKET

    # Sanitise the ISO timestamp so it is safe for object keys
    safe_ts = timestamp.replace(":", "-")
    object_key = f"{task_id}/{execution_id}/step_{step_index:03d}_{safe_ts}.png"

    client.put_object(
        bucket_name=bucket,
        object_name=object_key,
        data=io.BytesIO(png_bytes),
        length=len(png_bytes),
        content_type="image/png",
    )
    return object_key


async def upload_screenshot(
    task_id: str,
    execution_id: str,
    step_index: int,
    png_bytes: bytes,
    timestamp: str,
) -> str:
    """Upload a PNG screenshot to MinIO. Returns the object key.

    The object key follows the pattern::

        {task_id}/{execution_id}/step_{step_index:03d}_{timestamp}.png

    Colons in *timestamp* are replaced with hyphens so the key is safe
    for all S3-compatible stores.

    The target bucket is created automatically if it does not exist.

    Parameters
    ----------
    task_id:
        Identifier for the automation task.
    execution_id:
        Identifier for this particular execution run.
    step_index:
        Zero-based index of the step within the execution.
    png_bytes:
        Raw PNG image data.
    timestamp:
        ISO-8601 timestamp string used in the object key.

    Returns
    -------
    str
        The object key under which the screenshot was stored.
    """
    await ensure_bucket()
    return await asyncio.to_thread(
        _upload_screenshot_sync,
        task_id,
        execution_id,
        step_index,
        png_bytes,
        timestamp,
    )


def get_presigned_url(object_key: str, expires_seconds: int = 3600) -> str:
    """Return a presigned GET URL for the given object key.

    Parameters
    ----------
    object_key:
        The MinIO object key (as returned by :func:`upload_screenshot`).
    expires_seconds:
        How long the URL should remain valid.  Defaults to one hour.

    Returns
    -------
    str
        A presigned HTTP URL that grants temporary read access.
    """
    client = get_client()
    bucket = get_settings().MINIO_BUCKET
    return client.presigned_get_object(
        bucket_name=bucket,
        object_name=object_key,
        expires=timedelta(seconds=expires_seconds),
    )
