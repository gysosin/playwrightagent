"""Playwright MCP client that connects to the containerized server over SSE.

The Playwright MCP server runs as a Docker container and exposes an SSE
transport at ``http://<host>:8931/sse``.  This module provides
:class:`PlaywrightMCPClient`, an async context-manager that manages the
session lifecycle and offers convenience wrappers for common browser actions.

Usage::

    from mcp_client.playwright_client import PlaywrightMCPClient

    async with PlaywrightMCPClient() as pw:
        await pw.navigate("https://example.com")
        text = await pw.get_text("h1")
        png = await pw.screenshot()
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import ImageContent, TextContent

from config import get_settings

logger = logging.getLogger(__name__)

# Default pulled from application settings; can be overridden per-instance.
_DEFAULT_URL: str = get_settings().PLAYWRIGHT_MCP_URL


async def get_playwright_session() -> ClientSession:
    """Connect to the Playwright MCP server and return a ready session.

    .. note::
        This is a *low-level* helper.  Prefer :class:`PlaywrightMCPClient` for
        most use-cases because it manages the session lifecycle for you.

    The caller is responsible for entering the ``sse_client`` async context
    and keeping it alive for the lifetime of the session.
    """
    settings = get_settings()
    read_stream, write_stream = sse_client(settings.PLAYWRIGHT_MCP_URL)
    # The streams are async-context-manager results; in practice use
    # PlaywrightMCPClient which handles the full lifecycle.
    session = ClientSession(read_stream, write_stream)
    await session.initialize()
    return session


class PlaywrightMCPClient:
    """Async context-manager for Playwright MCP sessions.

    On entry the client connects to the SSE endpoint, initialises the MCP
    session, and optionally logs the tools advertised by the server.

    Parameters
    ----------
    base_url:
        SSE endpoint URL of the Playwright MCP server.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url: str = base_url or _DEFAULT_URL
        self._session: ClientSession | None = None
        # Internal references kept so we can tear down cleanly.
        self._sse_context: Any = None
        self._session_context: Any = None

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> PlaywrightMCPClient:
        """Connect and initialise the MCP session."""
        logger.info("Connecting to Playwright MCP at %s", self.base_url)

        self._sse_context = sse_client(self.base_url)
        streams = await self._sse_context.__aenter__()
        read_stream, write_stream = streams

        self._session = ClientSession(read_stream, write_stream)
        self._session_context = self._session
        await self._session_context.__aenter__()
        await self._session.initialize()

        logger.info("Playwright MCP session initialised")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Close the MCP session and SSE transport."""
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                logger.debug("Error closing MCP session", exc_info=True)
            self._session_context = None
            self._session = None

        if self._sse_context is not None:
            try:
                await self._sse_context.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                logger.debug("Error closing SSE transport", exc_info=True)
            self._sse_context = None

    # ------------------------------------------------------------------
    # Generic tool invocation
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a Playwright MCP tool and return the result content.

        Parameters
        ----------
        tool_name:
            The MCP tool name (e.g. ``browser_navigate``).
        arguments:
            Key/value arguments expected by the tool.

        Returns
        -------
        The :class:`mcp.types.CallToolResult` object.  Raises
        :class:`RuntimeError` if the result indicates an error.
        """
        if self._session is None:
            raise RuntimeError("PlaywrightMCPClient is not connected; use 'async with' block")

        logger.debug("Calling MCP tool %s(%s)", tool_name, arguments)
        result = await self._session.call_tool(tool_name, arguments or {})

        if result.isError:
            error_text = _extract_text(result.content)
            raise RuntimeError(f"Playwright MCP tool '{tool_name}' returned an error: {error_text}")

        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools advertised by the server.

        Useful for introspection and debugging.
        """
        if self._session is None:
            raise RuntimeError("PlaywrightMCPClient is not connected; use 'async with' block")

        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
            for t in result.tools
        ]

    # ------------------------------------------------------------------
    # Convenience wrappers for common browser actions
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> Any:
        """Navigate to *url*."""
        return await self.call_tool("browser_navigate", {"url": url})

    async def click(self, element: str, ref: str | None = None) -> Any:
        """Click an element.

        The ``@playwright/mcp`` server accepts either a ``selector`` string
        or an ``element``/``ref`` pair depending on version.  This wrapper
        sends both ``element`` and ``ref`` when *ref* is provided, otherwise
        it falls back to passing *element* as the selector.
        """
        args: dict[str, Any] = {"element": element}
        if ref is not None:
            args["ref"] = ref
        return await self.call_tool("browser_click", args)

    async def fill(self, element: str, value: str, ref: str | None = None) -> Any:
        """Type *value* into an input field identified by *element*."""
        args: dict[str, Any] = {"element": element, "text": value}
        if ref is not None:
            args["ref"] = ref
        return await self.call_tool("browser_type", args)

    async def wait_for(self, selector: str, timeout_ms: int = 5000) -> dict:
        """Wait for an element to appear."""
        return await self.call_tool("browser_wait_for_selector", {"selector": selector, "timeout": timeout_ms})

    async def screenshot(self) -> bytes:
        """Take a screenshot and return raw PNG bytes."""
        result = await self.call_tool("browser_screenshot", {})
        for block in result.content:
            if isinstance(block, ImageContent):
                return base64.b64decode(block.data)
        # Fallback: some versions return base64 data inside a TextContent.
        for block in result.content:
            if isinstance(block, TextContent) and block.text:
                try:
                    return base64.b64decode(block.text)
                except Exception:
                    pass
        raise RuntimeError("Screenshot result did not contain decodable image data")

    async def get_text(self, selector: str) -> str:
        """Get text content of an element identified by *selector*."""
        result = await self.call_tool("browser_get_text", {"selector": selector})
        return _extract_text(result.content)

    async def close_browser(self) -> Any:
        """Close the browser context."""
        return await self.call_tool("browser_close", {})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_text(content: list[Any]) -> str:
    """Pull plain text out of a list of MCP content blocks."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return "\n".join(parts)
