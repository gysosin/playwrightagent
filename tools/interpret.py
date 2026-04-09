"""Interpret natural language steps into structured Playwright actions.

Uses OpenRouter LLM (via the OpenAI SDK) to convert free-form text into a
JSON list of browser automation actions.  Results are persisted in PostgreSQL
so that subsequent runs of the same task skip the LLM call entirely.
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from config import get_settings
from db.queries import (
    create_step_sequence,
    create_task,
    get_active_sequence,
    get_task_by_name,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a browser automation expert. You can automate any website. Convert the \
user's natural language into a JSON array of browser actions.

Actions available:
  navigate  — go to a URL. Fields: "url"
  click     — click something (fails if not found). Fields: "selector"
  try_click — click if element exists, skip silently if not. Fields: "selector"
              Use for optional elements: cookie banners, popups, dismiss buttons.
  hover     — move mouse over an element (for hover menus/effects). Fields: "selector"
  fill      — type into an input. Fields: "selector", "value"
  wait_for  — pause before continuing. Fields: "wait_seconds" (number) and/or "wait_text" (visible text to wait for)
  screenshot — capture the screen. No extra fields.
  get_text  — read visible text. Fields: "selector"
  close     — close the browser. No extra fields.

Every action object must also have "action" and "description" (human-readable).

Selector tips — pick whatever works best for the site:
  "text=Sign In"           visible text (most reliable for buttons/links/tabs)
  "#email"                 ID selector
  "input[name='q']"        attribute selector
  "h1", "body", "table"   tag selectors
  ".class-name"            class selector (only when you're confident it exists)

Think about what a human would actually see and click on the page. Use visible \
text selectors when the element has readable text. Use "body" for get_text when \
you need to read the whole page. Add a short wait_for after navigation or clicks \
that trigger page loads.

Return ONLY a valid JSON array. No markdown, no explanation.\
"""


def _build_openai_client() -> AsyncOpenAI:
    """Return an AsyncOpenAI client configured for OpenRouter."""
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
    )


async def _call_llm(nl_steps: str) -> list[dict]:
    """Send *nl_steps* to the LLM and return parsed action list."""
    client = _build_openai_client()
    settings = get_settings()

    response = await client.chat.completions.create(
        model=settings.OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": nl_steps},
        ],
        temperature=0.0,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if the model wraps output despite instructions.
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Remove first line (```json or ```) and last line (```)
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        raw = "\n".join(lines)

    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON: %s", raw)
        raise ValueError(f"LLM returned invalid JSON: {exc}") from exc

    if not isinstance(steps, list):
        raise ValueError(f"Expected a JSON array from LLM, got {type(steps).__name__}")

    return steps


async def interpret_steps(task_name: str, nl_steps: str) -> dict:
    """Interpret natural language steps into structured Playwright actions and save to DB.

    If a task with this name already exists and has an active step sequence,
    return the existing sequence WITHOUT calling the LLM.

    If new, call OpenRouter LLM to convert *nl_steps* into a JSON list of
    actions, save as a new task + step_sequence (revision 1), return the
    sequence.

    Args:
        task_name: Unique human-readable name for the automation task.
        nl_steps: Free-form natural language describing the browser steps.

    Returns:
        A dict with keys: task_id, sequence_id, revision, steps, cached.
    """
    # Check for an existing task with an active sequence.
    task = await get_task_by_name(task_name)
    if task is not None:
        # Only return cached if the nl_steps haven't changed.
        stored_nl = task.get("nl_steps", "")
        if stored_nl == nl_steps:
            sequence = await get_active_sequence(str(task["id"]))
            if sequence is not None:
                logger.info("Returning cached sequence for task %r", task_name)
                steps = sequence["steps"]
                if isinstance(steps, str):
                    steps = json.loads(steps)
                return {
                    "task_id": str(task["id"]),
                    "sequence_id": str(sequence["id"]),
                    "revision": sequence["revision"],
                    "steps": steps,
                    "cached": True,
                }
        else:
            logger.info("nl_steps changed for task %r — re-interpreting", task_name)

    # Call the LLM to interpret.
    logger.info("Calling LLM to interpret steps for task %r", task_name)
    steps = await _call_llm(nl_steps)

    # Persist: create the task if it doesn't exist yet, then the sequence.
    if task is None:
        task = await create_task(task_name, nl_steps)

    task_id = str(task["id"])
    next_rev = 1
    # If task already exists, deactivate old sequences and bump revision.
    if task.get("nl_steps", "") != nl_steps:
        from db.queries import deactivate_all_sequences, get_next_revision
        next_rev = await get_next_revision(task_id)
        await deactivate_all_sequences(task_id)

    sequence = await create_step_sequence(task_id, revision=next_rev, steps=steps)

    return {
        "task_id": task_id,
        "sequence_id": str(sequence["id"]),
        "revision": sequence["revision"],
        "steps": steps,
        "cached": False,
    }
