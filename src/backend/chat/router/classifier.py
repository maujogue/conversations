"""Router classifier: one structured-output call labelling the user's turn (spec 4)."""

import asyncio
import dataclasses
import logging
import time

from django.conf import settings

from chat.agents.base import BaseAgent
from chat.enums import RoutingDomain, RoutingReason, RoutingTask, RoutingTier

from .labels import RoutingLabels
from .prompts import DEFAULT_ROUTER_PROMPT, ROUTER_PROMPT_NAME

logger = logging.getLogger(__name__)

# A message shorter than this (in whitespace-separated words) with previous labels
# is treated as a follow-up and reuses them without calling the model.
SHORTCUT_MAX_WORDS = 6
# Bounds on the text sent to the router model; the classifier does not need more.
MESSAGE_MAX_CHARS = 2000
PREVIOUS_ANSWER_MAX_CHARS = 800  # ~200 tokens
# Bound on a cold Langfuse prompt fetch. The SDK caches prompts in memory and
# refreshes them in the background, so this only bites on the first call.
PROMPT_FETCH_TIMEOUT_S = 2

FALLBACK_LABELS = RoutingLabels(
    complexity=RoutingTier.STANDARD,
    domain=RoutingDomain.GENERAL,
    task=RoutingTask.QA_KNOWLEDGE,
    confidence=0.0,
)


def router_model_hrid() -> str:
    """Return the HRID of the model running the classifier."""
    return settings.LLM_ROUTER_MODEL_HRID or settings.LLM_DEFAULT_MODEL_HRID


def get_router_prompt() -> tuple[str, str | None]:
    """Return the classifier system prompt and its Langfuse version, if any.

    Uses Langfuse prompt management when enabled, with the local default as
    fallback. A Langfuse outage never breaks classification: any error yields the
    local default with no version.
    """
    if not settings.LANGFUSE_ENABLED:
        return DEFAULT_ROUTER_PROMPT, None

    try:
        from langfuse import get_client  # noqa: PLC0415

        prompt = get_client().get_prompt(
            ROUTER_PROMPT_NAME,
            label="production",
            type="text",
            fallback=DEFAULT_ROUTER_PROMPT,
            max_retries=0,
            fetch_timeout_seconds=PROMPT_FETCH_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Router prompt fetch from Langfuse failed, using default", exc_info=True)
        return DEFAULT_ROUTER_PROMPT, None

    text = getattr(prompt, "prompt", None) or DEFAULT_ROUTER_PROMPT
    if getattr(prompt, "is_fallback", False):
        return text, None
    version = getattr(prompt, "version", None)
    return text, str(version) if version is not None else None


@dataclasses.dataclass(init=False)
class RouterClassifierAgent(BaseAgent):
    """Structured-output agent producing `RoutingLabels`. No tools."""

    def __init__(self, *, system_prompt: str, **kwargs):
        self._router_system_prompt = system_prompt
        super().__init__(
            model_hrid=router_model_hrid(),
            output_type=RoutingLabels,
            retries=kwargs.pop("retries", 1),
            **kwargs,
        )

    def get_tools(self):
        return []

    def get_system_prompt(self):
        return self._router_system_prompt


def build_classifier_agent(system_prompt: str) -> RouterClassifierAgent:
    """Build the classifier agent. Kept as a function so tests can swap the model."""
    return RouterClassifierAgent(system_prompt=system_prompt)


def build_user_prompt(
    message_text: str,
    previous_labels: RoutingLabels | None,
    previous_answer_excerpt: str | None,
    has_attachments: bool,
    has_project_context: bool,
) -> str:
    """Assemble the user-side input of the classifier call."""
    parts = []
    if previous_labels is not None:
        parts.append(
            "Previous turn labels: "
            f"complexity={previous_labels.complexity.value}, "
            f"domain={previous_labels.domain.value}, "
            f"task={previous_labels.task.value}."
        )
    if previous_answer_excerpt:
        excerpt = previous_answer_excerpt[-PREVIOUS_ANSWER_MAX_CHARS:]
        parts.append(f"End of the previous assistant answer:\n<<<\n{excerpt}\n>>>")
    parts.append(f"Attachments present: {'yes' if has_attachments else 'no'}.")
    parts.append(f"Project context present: {'yes' if has_project_context else 'no'}.")
    parts.append(f"User message:\n<<<\n{message_text[:MESSAGE_MAX_CHARS]}\n>>>")
    return "\n\n".join(parts)


async def classify(  # noqa: PLR0913
    message_text: str,
    previous_labels: RoutingLabels | None,
    previous_answer_excerpt: str | None,
    has_attachments: bool,
    has_project_context: bool,
    forced_web_search: bool,
) -> tuple[RoutingLabels, str, int, str | None]:
    """Label the user's turn.

    Returns `(labels, reason, latency_ms, prompt_version)`. `latency_ms` covers the
    whole classification (prompt fetch, agent build, model call); the router
    timeout bounds the model call only. `reason` is one of
    `RoutingReason.SHORTCUT` (model call skipped), `RoutingReason.CLASSIFIED` or
    `RoutingReason.FALLBACK` (timeout or error: previous labels, else
    standard/general/qa_knowledge with confidence 0).

    When web search is forced and there is no other signal (no attachments, no
    project context), the task is `research` whatever the model says; the model is
    still called for the complexity.
    """
    word_count = len(message_text.split())
    if previous_labels is not None and word_count < SHORTCUT_MAX_WORDS:
        labels = previous_labels.model_copy(
            update={"task": RoutingTask.CONVERSATION, "confidence": 1.0}
        )
        logger.debug("Router classifier shortcut (words=%d)", word_count)
        return labels, RoutingReason.SHORTCUT.value, 0, None

    force_research = forced_web_search and not has_attachments and not has_project_context

    user_prompt = build_user_prompt(
        message_text,
        previous_labels,
        previous_answer_excerpt,
        has_attachments,
        has_project_context,
    )

    started = time.perf_counter()
    prompt_version = None
    try:
        # The prompt fetch and the agent build are kept outside the cancellable
        # window: cancelling a task in the middle of a lazy import leaves modules
        # half-initialised. Only the model call is bounded by the router timeout.
        system_prompt, prompt_version = await asyncio.to_thread(get_router_prompt)
        agent = build_classifier_agent(system_prompt)
        result = await asyncio.wait_for(
            agent.run(user_prompt), timeout=settings.LLM_ROUTER_TIMEOUT_S
        )
        labels = result.output
        reason = RoutingReason.CLASSIFIED
    except TimeoutError:
        labels = previous_labels or FALLBACK_LABELS
        reason = RoutingReason.FALLBACK
        prompt_version = None
        logger.debug(
            "Router classifier timed out after %.0f ms", settings.LLM_ROUTER_TIMEOUT_S * 1000
        )
    except Exception:  # noqa: BLE001
        labels = previous_labels or FALLBACK_LABELS
        reason = RoutingReason.FALLBACK
        prompt_version = None
        logger.debug("Router classifier failed, falling back", exc_info=True)
    latency_ms = int((time.perf_counter() - started) * 1000)

    if force_research:
        labels = labels.model_copy(update={"task": RoutingTask.RESEARCH})

    logger.debug(
        "Router classifier outcome reason=%s complexity=%s confidence=%.2f latency_ms=%d",
        reason.value,
        labels.complexity.value,
        labels.confidence,
        latency_ms,
    )
    return labels, reason.value, latency_ms, prompt_version
