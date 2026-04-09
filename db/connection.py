"""Asyncpg connection pool and convenience query helpers.

Usage::

    from db.connection import init_pool, close_pool, fetch, fetchrow, execute

    # On application startup
    await init_pool()

    # During request handling
    rows = await fetch("SELECT * FROM tasks")

    # On application shutdown
    await close_pool()
"""

from __future__ import annotations

import asyncpg

from config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the asyncpg connection pool.

    Must be called exactly once during application startup.  Subsequent
    calls are silently ignored so that the function is idempotent.
    """
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(dsn=get_settings().postgres_dsn)


async def get_pool() -> asyncpg.Pool:
    """Return the initialised connection pool.

    Raises:
        RuntimeError: If :func:`init_pool` has not been called yet.
    """
    if _pool is None:
        raise RuntimeError(
            "Connection pool is not initialised. Call init_pool() first."
        )
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ------------------------------------------------------------------
# Convenience wrappers
# ------------------------------------------------------------------

async def execute(query: str, *args: object) -> str:
    """Execute a query and return the status string (e.g. ``INSERT 0 1``)."""
    pool = await get_pool()
    return await pool.execute(query, *args)


async def fetch(query: str, *args: object) -> list[asyncpg.Record]:
    """Execute a query and return all resulting rows."""
    pool = await get_pool()
    return await pool.fetch(query, *args)


async def fetchrow(query: str, *args: object) -> asyncpg.Record | None:
    """Execute a query and return the first row, or ``None``."""
    pool = await get_pool()
    return await pool.fetchrow(query, *args)
