"""ADK agent entry point — Approach C + SOP replay.

The agent drives Playwright MCP directly, with automatic screenshot storage
and SOP playbook recording/replay.

Run with:
    adk web agents/
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams

from tools.session_tools import start_session, end_session, get_session_history
from tools.sop_tools import load_sop_playbook, save_sop_playbook, record_sop_from_execution
from tools.auto_screenshot import after_browser_action

_settings_model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")
_settings_key = os.environ.get("OPENROUTER_API_KEY", "")
_settings_base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_playwright_url = os.environ.get("PLAYWRIGHT_MCP_URL", "http://localhost:8931/sse")

root_agent = Agent(
    name="playwright_agent",
    model=LiteLlm(
        model=f"openrouter/{_settings_model}",
        api_key=_settings_key,
        api_base=_settings_base_url,
    ),
    description="Browser automation agent with SOP playbook replay and auto-healing.",
    instruction="""\
You are a browser automation agent. You control a real browser and complete tasks the user describes.

## How you see and interact with pages

- **browser_snapshot** (with depth=3) — returns the page's accessibility tree with ref numbers. This is how you see what's on the page. ALWAYS call this before interacting.
- **browser_click** — click an element. Requires "ref" from the snapshot and "element" (description).
- **browser_type** — type text. Requires "ref" and "text".
- **browser_hover** — hover over an element. Requires "ref".
- **browser_navigate** — go to a URL.
- **browser_take_screenshot** — capture a screenshot (auto-saved to storage). Call after every action.
- **browser_wait_for** — wait for text to appear or time to pass.

## How you think

You are like a human sitting at a computer. For every action:
1. Look at the page (browser_snapshot with depth=3)
2. Find the element you need by reading the snapshot
3. Act on it using its ref number
4. Take a screenshot (browser_take_screenshot)
5. Look again to see what changed

If something blocks you (popup, banner, overlay), deal with it first then continue.
If an action fails, look at the page again and try a different approach.

## Completing the user's intent

Read the user's request carefully. Every step they describe must be performed.
If they ask you to find, read, extract, or return a value — you must actually
navigate to where that value is, read it from the page, and include the real
value in your final response. Never summarize without completing all steps.

## Session and SOP tools

- **start_session(task_name)** — call at the start. Enables auto-logging.
- **end_session(execution_id, status, summary)** — call when done.
- **load_sop_playbook(sop_id)** — if the user mentions an SOP ID, check for a saved playbook first.
- **save_sop_playbook(sop_id, steps)** — after success, save the flow as semantic steps (human-readable descriptions, not ref numbers) so it can be replayed next time.
- **record_sop_from_execution(sop_id, execution_id)** — auto-record from session logs.

When an SOP ID is mentioned:
1. Load the playbook. If it exists, follow the saved steps (snapshot → find element → act for each).
2. If no playbook, figure it out yourself, then save the playbook after success.
3. If replay fails because the site changed, adapt and save the updated playbook.
""",
    after_tool_callback=after_browser_action,
    tools=[
        MCPToolset(
            connection_params=SseConnectionParams(url=_playwright_url),
        ),
        start_session,
        end_session,
        get_session_history,
        load_sop_playbook,
        save_sop_playbook,
        record_sop_from_execution,
    ],
)
