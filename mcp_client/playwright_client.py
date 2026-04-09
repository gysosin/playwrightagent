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
        """Call a Playwright MCP tool and return the result content."""
        if self._session is None:
            raise RuntimeError("PlaywrightMCPClient is not connected; use 'async with' block")

        logger.debug("Calling MCP tool %s(%s)", tool_name, arguments)
        result = await self._session.call_tool(tool_name, arguments or {})

        if result.isError:
            error_text = _extract_text(result.content)
            raise RuntimeError(f"Playwright MCP tool '{tool_name}' returned an error: {error_text}")

        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools advertised by the server."""
        if self._session is None:
            raise RuntimeError("PlaywrightMCPClient is not connected; use 'async with' block")

        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
            for t in result.tools
        ]

    # ------------------------------------------------------------------
    # Convenience wrappers — use browser_run_code for selector-based ops
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> Any:
        """Navigate to *url*."""
        return await self.call_tool("browser_navigate", {"url": url})

    async def click(self, selector: str, timeout_ms: int = 10000) -> Any:
        """Click an element by CSS selector using Playwright code."""
        escaped = selector.replace("\\", "\\\\").replace("`", "\\`")
        code = f"async (page) => {{ await page.locator(`{escaped}`).click({{ timeout: {timeout_ms} }}); }}"
        return await self.call_tool("browser_run_code", {"code": code})

    async def hover(self, selector: str, timeout_ms: int = 10000) -> Any:
        """Hover over an element by CSS selector."""
        escaped = selector.replace("\\", "\\\\").replace("`", "\\`")
        code = f"async (page) => {{ await page.locator(`{escaped}`).hover({{ timeout: {timeout_ms} }}); }}"
        return await self.call_tool("browser_run_code", {"code": code})

    async def fill(self, selector: str, value: str, timeout_ms: int = 10000) -> Any:
        """Type *value* into an input field by CSS selector."""
        escaped_sel = selector.replace("\\", "\\\\").replace("`", "\\`")
        escaped_val = value.replace("\\", "\\\\").replace("`", "\\`")
        code = f"async (page) => {{ await page.locator(`{escaped_sel}`).fill(`{escaped_val}`, {{ timeout: {timeout_ms} }}); }}"
        return await self.call_tool("browser_run_code", {"code": code})

    async def wait_time(self, seconds: float) -> Any:
        """Wait for a fixed number of seconds."""
        return await self.call_tool("browser_wait_for", {"time": seconds})

    async def wait_for_text(self, text: str, timeout_ms: int = 10000) -> Any:
        """Wait for specific text to appear on the page."""
        escaped = text.replace("\\", "\\\\").replace("`", "\\`")
        code = f"async (page) => {{ await page.getByText(`{escaped}`).first().waitFor({{ timeout: {timeout_ms} }}); }}"
        return await self.call_tool("browser_run_code", {"code": code})

    async def screenshot(self) -> bytes:
        """Take a screenshot and return raw PNG bytes."""
        result = await self.call_tool("browser_take_screenshot", {"type": "png"})
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
        """Get text content of an element by CSS selector."""
        escaped = selector.replace("\\", "\\\\").replace("`", "\\`")
        code = f"async (page) => {{ return await page.locator(`{escaped}`).innerText({{ timeout: 10000 }}); }}"
        result = await self.call_tool("browser_run_code", {"code": code})
        return _extract_text(result.content)

    async def snapshot(self) -> str:
        """Get an accessibility snapshot of the current page."""
        result = await self.call_tool("browser_snapshot", {})
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
