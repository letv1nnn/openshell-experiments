"""LLM inference via OpenAI-compatible Chat Completions API (vLLM, OpenAI, etc.).

Analyzes meeting notes and produces a structured leadership report JSON.

Environment variables:
    OPENAI_BASE_URL   Inference endpoint (default: https://inference.local/v1 for OpenShell)
    OPENAI_API_KEY    API key ("none" for unauthenticated vLLM)
    MODEL_ID          Model identifier (e.g. meta-llama/Llama-3.1-8B-Instruct)
"""

import json
import os
from datetime import datetime, timezone

from openai import APIConnectionError, APIStatusError, OpenAI

from .errors import AgentError
from .report import validate_report

SYSTEM_PROMPT = """\
You are a leadership report writer. You will receive meeting notes and must \
produce a structured JSON report.

Output ONLY valid JSON with this exact structure:
{
  "date": "<today's date as Mon DD, YYYY>",
  "summary": "<one paragraph, 2-3 sentences, capturing overall team status>",
  "bullets": [
    {"label": "Category Name:", "text": " Detail text with attribution. (Person)\\n"},
    ...
  ]
}

Rules for the report content:
- Be specific. Reference concrete deliverables, epics, components, and release targets.
- Attribute work. Name the people responsible in parentheses at the end of each bullet.
- State confidence levels. If confidence or risk was discussed, include it.
- Flag blockers and dependencies. Call out anything depending on external teams or at risk.
- Keep it concise. Each bullet should be 1-3 sentences. No filler.
- Use outcome-focused language. Lead with what's done or happening, not process narration.
- Use categories that naturally emerge from the meeting topics (component names, workstreams, \
or cross-cutting concerns like Documentation, Testing, Process).
- Order bullets by importance: most critical first, process/meta last.

Rules for the JSON format:
- "label" must end with ":"
- "text" must start with a space and end with "\\n"
- Do not include any text outside the JSON object. No markdown fences, no commentary.\
"""


INFERENCE_LOCAL = "https://inference.local/v1"


def _get_client() -> tuple[OpenAI, str]:
    base_url = os.environ.get("OPENAI_BASE_URL", INFERENCE_LOCAL)
    api_key = os.environ.get("OPENAI_API_KEY", "none")
    model_id = os.environ.get("MODEL_ID")

    if not model_id:
        raise AgentError("MODEL_ID not set.", retriable=False)

    client = OpenAI(base_url=base_url.rstrip("/"), api_key=api_key)
    return client, model_id


def analyze_notes(notes_text: str) -> dict:
    """Send meeting notes to the LLM and return the structured report dict.

    Raises AgentError on failure.
    """
    client, model_id = _get_client()

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    user_message = (
        f"Today's date is {today}.\n\n"
        f"Meeting notes:\n\n{notes_text}"
    )

    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except APIConnectionError as e:
        raise AgentError(f"Cannot reach inference endpoint: {e}", retriable=True) from e
    except APIStatusError as e:
        if e.status_code >= 500:
            raise AgentError(f"Server error ({e.status_code}). Retry later.", retriable=True) from e
        raise AgentError(f"API error ({e.status_code}): {e.message}", retriable=False) from e

    content = response.choices[0].message.content
    if not content:
        raise AgentError("Empty response from model.", retriable=False)

    try:
        report = json.loads(content)
    except json.JSONDecodeError as e:
        raise AgentError(f"Model returned invalid JSON: {e}\nRaw response: {content[:500]}", retriable=False) from e

    validate_report(report)
    return report
