"""
LLM client for the Self-Healing Ops Agent, routed through OpenRouter —
same unified endpoint pattern as the Phase 2 embeddings client.
"""

import json
import os
from dotenv import load_dotenv
from openai import OpenAI
load_dotenv()
_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API"],
    max_retries=5,  # openai SDK auto-retries 429s with backoff before we even see them
)

# Provider-prefixed model name, same convention as your embeddings config.
DIAGNOSIS_MODEL = os.environ.get("DIAGNOSIS_MODEL", "z-ai/glm-5.2:free")

# OpenRouter's `models` fallback array: if DIAGNOSIS_MODEL errors (rate-limited,
# down, moderation refusal), OpenRouter automatically retries with the next
# model here, in order — no extra retry code needed on our side for this case.
# Comma-separated so it's configurable via env without touching code.
DIAGNOSIS_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "DIAGNOSIS_FALLBACK_MODELS",
        "minimax/minimax-m3:free,nvidia/nemotron-3-ultra-550b-a55b:free",
    ).split(",")
    if m.strip()
]


def _strip_markdown_fence(content: str) -> str:
    """Some models wrap JSON output in ```json ... ``` regardless of
    response_format — strip that before parsing rather than failing on it."""
    stripped = content.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (with optional language tag) and the closing fence.
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def call_llm(prompt: str, response_format: str = "json") -> dict:
    """Single chat completion call. Returns parsed JSON when
    response_format='json'. Not every OpenRouter model supports the
    'json_object' response_format — check your chosen model's page on
    openrouter.ai/models before relying on this in production."""
    completion = _client.chat.completions.create(
        model=DIAGNOSIS_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"} if response_format == "json" else None,
        extra_body={"models": DIAGNOSIS_FALLBACK_MODELS} if DIAGNOSIS_FALLBACK_MODELS else {},
    )

    content = completion.choices[0].message.content
    used_model = completion.model  # which model in the chain actually served this

    if response_format != "json":
        return {"content": content}

    try:
        parsed = json.loads(_strip_markdown_fence(content))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Model {used_model} did not return valid JSON. "
            f"Confirm it supports response_format=json_object on OpenRouter. "
            f"Raw content: {content[:500]}"
        ) from e

    # Valid JSON doesn't mean it matched OUR schema — free models especially
    # will sometimes ignore the requested shape. Fail loudly and specifically
    # here rather than letting a bare KeyError/TypeError surface deep inside
    # a LangGraph node later.
    missing = [k for k in ("diagnosis", "remediation_plan") if k not in parsed]
    if missing:
        raise ValueError(
            f"Model {used_model} returned JSON but was missing required key(s) "
            f"{missing}. Raw response: {json.dumps(parsed)[:500]}"
        )

    diagnosis = parsed["diagnosis"]
    if not isinstance(diagnosis.get("confidence"), (int, float)):
        raise ValueError(
            f"Model {used_model} returned diagnosis.confidence as "
            f"{diagnosis.get('confidence')!r} (type {type(diagnosis.get('confidence')).__name__}), "
            f"expected a float between 0.0 and 1.0."
        )

    for i, step in enumerate(parsed["remediation_plan"].get("steps", [])):
        step_missing = [k for k in ("step_number", "action", "reversible") if k not in step]
        if step_missing:
            raise ValueError(
                f"Model {used_model} returned remediation_plan.steps[{i}] missing "
                f"required key(s) {step_missing}. Got keys: {list(step.keys())}"
            )
        if not isinstance(step["reversible"], bool):
            raise ValueError(
                f"Model {used_model} returned steps[{i}].reversible as "
                f"{step['reversible']!r}, expected true/false."
            )

    return parsed