"""Auto-heal broken Playwright steps using the LLM.

When a step fails during execution (e.g. the target website changed its
markup), this module asks the LLM to produce a corrected action based on the
error message and a screenshot of the current page state.

:func:`heal_step` is intentionally **not** decorated as an ADK tool — it is
called directly from the executor as a regular async function.
"""

from __future__ import annotations

import base64
import json
import logging

from openai import AsyncOpenAI

from config import get_settings
from db.queries import (
    create_revision,
    create_step_sequence,
    deactivate_all_sequences,
    get_next_revision,
)

logger = logging.getLogger(__name__)

_HEALING_SYSTEM_PROMPT = """\
You are a browser automation expert. A Playwright action failed because the \
website has changed.

You will be given:
1. The original step description
2. The error that occurred
3. A screenshot of the current page state

Your job: generate a NEW JSON action object that accomplishes the same goal \
as the original step but works with the current page state.

Return ONLY a single JSON object (the new action), no explanation.
Format: {"action": "...", "selector": "...", "value": "...", "url": "...", "description": "..."}\
"""


def _build_openai_client() -> AsyncOpenAI:
    """Return an AsyncOpenAI client configured for OpenRouter."""
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
    )


async def _call_llm_heal(
    failed_step: dict,
    error_message: str,
    screenshot_b64: str,
) -> dict:
    """Ask the LLM to produce a replacement action for a failed step."""
    client = _build_openai_client()
    settings = get_settings()

    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Original step description: {failed_step.get('description', 'N/A')}\n"
                f"Original action: {json.dumps(failed_step)}\n"
                f"Error: {error_message}"
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
            },
        },
    ]

    response = await client.chat.completions.create(
        model=settings.OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": _HEALING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present.
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        raw = "\n".join(lines)

    try:
        new_action = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Healer LLM returned invalid JSON: %s", raw)
        raise ValueError(f"Healer LLM returned invalid JSON: {exc}") from exc

    if not isinstance(new_action, dict):
        raise ValueError(
            f"Expected a JSON object from healer LLM, got {type(new_action).__name__}"
        )

    return new_action


async def heal_step(
    task_id: str,
    execution_id: str,
    sequence_id: str,
    steps: list[dict],
    failed_step_index: int,
    failed_step: dict,
    error_message: str,
    current_page_screenshot: bytes,
) -> dict:
    """Auto-heal a broken step using the LLM.

    NOT decorated as an ADK tool — called directly from the executor.

    1. Call the LLM with the original step description, the error, and a
       screenshot of the current page state (as base64).
    2. LLM returns a new action for this specific step.
    3. Deactivate old sequence, create new sequence with the healed step
       replacing the failed one.
    4. Create a revision record.
    5. Return the new sequence information.

    Args:
        task_id: UUID of the automation task.
        execution_id: UUID of the current execution.
        sequence_id: UUID of the step sequence that contained the broken step.
        steps: The full list of steps from the original sequence.
        failed_step_index: Zero-based index of the step that failed.
        failed_step: The action dict that failed.
        error_message: The error string captured during execution.
        current_page_screenshot: Raw PNG bytes of the current page.

    Returns:
        A dict with keys: new_sequence_id, new_steps, reason.
    """
    screenshot_b64 = base64.b64encode(current_page_screenshot).decode("ascii")

    logger.info(
        "Healing step %d for task %s (error: %s)",
        failed_step_index,
        task_id,
        error_message[:120],
    )

    new_action = await _call_llm_heal(failed_step, error_message, screenshot_b64)

    # Build the updated step list with the healed action replacing the failed one.
    new_steps = list(steps)  # shallow copy
    new_steps[failed_step_index] = new_action

    # Persist: deactivate old sequences, create the new one.
    next_rev = await get_next_revision(task_id)
    await deactivate_all_sequences(task_id)
    new_sequence = await create_step_sequence(task_id, revision=next_rev, steps=new_steps)
    new_sequence_id = str(new_sequence["id"])

    reason = (
        f"Step {failed_step_index} failed with: {error_message[:200]}. "
        f"Healed action: {json.dumps(new_action)}"
    )

    await create_revision(
        task_id=task_id,
        old_sequence_id=sequence_id,
        new_sequence_id=new_sequence_id,
        failed_step=failed_step_index,
        reason=reason,
    )

    logger.info(
        "Healed step %d → new sequence %s (revision %d)",
        failed_step_index,
        new_sequence_id,
        next_rev,
    )

    return {
        "new_sequence_id": new_sequence_id,
        "new_steps": new_steps,
        "reason": reason,
    }
