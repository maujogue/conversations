"""Tests for the router classifier (docs/llm-router-spec.md section 4)."""

import asyncio
from unittest import mock

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from chat.enums import RoutingDomain, RoutingReason, RoutingTask, RoutingTier
from chat.router import RoutingDecision, RoutingLabels, classify
from chat.router.classifier import (
    FALLBACK_LABELS,
    RouterClassifierAgent,
    build_user_prompt,
    get_router_prompt,
)
from chat.router.prompts import DEFAULT_ROUTER_PROMPT

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def generous_router_timeout(settings):
    """Keep the model-call timeout out of the way; timeout tests lower it explicitly."""
    settings.LLM_ROUTER_TIMEOUT_S = 5


PREVIOUS = RoutingLabels(
    complexity=RoutingTier.COMPLEX,
    domain=RoutingDomain.LEGAL,
    task=RoutingTask.REASONING,
    confidence=0.8,
)


def model_returns(labels: dict):
    """FunctionModel callback answering with `labels` as structured output."""

    def respond(_messages, info):
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=labels)])

    return respond


def agent_with(model, recorded_prompts=None):
    """Return a builder yielding a real RouterClassifierAgent with a swapped model."""

    def build(system_prompt):
        if recorded_prompts is not None:
            recorded_prompts.append(system_prompt)
        agent = RouterClassifierAgent(system_prompt=system_prompt)
        agent._model = model
        return agent

    return build


async def run_classify(**overrides):
    """Call classify with sensible defaults."""
    kwargs = {
        "message_text": "Explique la procédure de rupture conventionnelle dans la fonction publique",
        "previous_labels": None,
        "previous_answer_excerpt": None,
        "has_attachments": False,
        "has_project_context": False,
        "forced_web_search": False,
    }
    kwargs.update(overrides)
    return await classify(**kwargs)


# --- shortcuts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_follow_up_reuses_previous_labels_without_model_call():
    with mock.patch("chat.router.classifier.build_classifier_agent") as build:
        labels, reason, latency, version = await run_classify(
            message_text="Et pour les contractuels ?", previous_labels=PREVIOUS
        )
    build.assert_not_called()
    assert reason == RoutingReason.SHORTCUT
    assert latency == 0
    assert version is None
    assert labels.complexity == RoutingTier.COMPLEX
    assert labels.domain == RoutingDomain.LEGAL
    assert labels.task == RoutingTask.CONVERSATION
    assert labels.confidence == 1.0
    # previous labels are not mutated
    assert PREVIOUS.task == RoutingTask.REASONING


@pytest.mark.asyncio
async def test_short_message_without_previous_labels_calls_the_model():
    output = {
        "complexity": "simple",
        "domain": "general",
        "task": "conversation",
        "confidence": 0.95,
    }
    builder = agent_with(FunctionModel(model_returns(output)))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify(message_text="Bonjour !")
    assert reason == RoutingReason.CLASSIFIED
    assert labels.complexity == RoutingTier.SIMPLE


@pytest.mark.asyncio
async def test_six_word_message_is_not_a_shortcut():
    output = {"complexity": "standard", "domain": "hr", "task": "writing", "confidence": 0.8}
    builder = agent_with(FunctionModel(model_returns(output)))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        _, reason, _, _ = await run_classify(
            message_text="Rédige un mail pour mon équipe", previous_labels=PREVIOUS
        )
    assert reason == RoutingReason.CLASSIFIED


@pytest.mark.asyncio
async def test_forced_web_search_forces_research_task_but_keeps_classifier_complexity():
    output = {
        "complexity": "complex",
        "domain": "finance",
        "task": "qa_knowledge",
        "confidence": 0.9,
    }
    builder = agent_with(FunctionModel(model_returns(output)))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify(
            message_text="Quel est le dernier taux directeur de la BCE et ses effets ?",
            forced_web_search=True,
        )
    assert reason == RoutingReason.CLASSIFIED
    assert labels.task == RoutingTask.RESEARCH
    assert labels.complexity == RoutingTier.COMPLEX
    assert labels.confidence == 0.9


@pytest.mark.asyncio
async def test_forced_web_search_with_attachments_keeps_model_task():
    output = {"complexity": "standard", "domain": "legal", "task": "document_qa", "confidence": 0.8}
    builder = agent_with(FunctionModel(model_returns(output)))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, _, _, _ = await run_classify(
            message_text="Que dit ce décret sur les délais de recours ?",
            forced_web_search=True,
            has_attachments=True,
        )
    assert labels.task == RoutingTask.DOCUMENT_QA


@pytest.mark.asyncio
async def test_forced_web_search_applies_research_on_fallback_too(settings):
    settings.LLM_ROUTER_TIMEOUT_S = 0.01

    async def slow(_messages, _info):
        await asyncio.sleep(1)
        return ModelResponse(parts=[])

    builder = agent_with(FunctionModel(slow))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify(forced_web_search=True)
    assert reason == RoutingReason.FALLBACK
    assert labels.task == RoutingTask.RESEARCH
    assert labels.complexity == RoutingTier.STANDARD


# --- happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_model_labels_and_latency():
    output = {"complexity": "standard", "domain": "hr", "task": "qa_knowledge", "confidence": 0.85}
    recorded = []
    builder = agent_with(FunctionModel(model_returns(output)), recorded)
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, latency, version = await run_classify(
            previous_labels=PREVIOUS,
            previous_answer_excerpt="x" * 2000,
            has_project_context=True,
        )
    assert reason == RoutingReason.CLASSIFIED
    assert labels == RoutingLabels(
        complexity=RoutingTier.STANDARD,
        domain=RoutingDomain.HR,
        task=RoutingTask.QA_KNOWLEDGE,
        confidence=0.85,
    )
    assert latency >= 0
    assert version is None  # Langfuse disabled in tests
    assert recorded == [DEFAULT_ROUTER_PROMPT]


@pytest.mark.asyncio
async def test_agent_receives_context_in_user_prompt():
    seen = {}

    def respond(messages, info):
        seen["messages"] = messages
        seen["instructions"] = messages[0].instructions
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={
                        "complexity": "simple",
                        "domain": "general",
                        "task": "writing",
                        "confidence": 1.0,
                    },
                )
            ]
        )

    builder = agent_with(FunctionModel(respond))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        await run_classify(
            message_text="mot " * 1500,  # 6000 chars, many words: no shortcut
            previous_labels=PREVIOUS,
            previous_answer_excerpt="A" * 100 + "B" * 800,
            has_attachments=True,
        )
    user_text = "".join(
        part.content for part in seen["messages"][0].parts if hasattr(part, "content")
    )
    assert "complexity=complex, domain=legal, task=reasoning" in user_text
    excerpt_section = user_text.split("<<<", 1)[1].split(">>>", 1)[0]
    assert "A" not in excerpt_section  # excerpt truncated to its last 800 chars
    assert "B" * 800 in excerpt_section
    message_section = user_text.rsplit("<<<", 1)[1].split(">>>", 1)[0].strip("\n")
    assert len(message_section) == 2000  # message truncated
    assert "Attachments present: yes" in user_text
    assert "Project context present: no" in user_text
    # pydantic-ai normalises trailing whitespace of instructions
    assert seen["instructions"].strip() == DEFAULT_ROUTER_PROMPT.strip()


def test_build_user_prompt_without_context():
    text = build_user_prompt("Bonjour", None, None, False, False)
    assert "Previous turn labels" not in text
    assert "previous assistant answer" not in text
    assert "User message:\n<<<\nBonjour\n>>>" in text


@pytest.mark.asyncio
async def test_agent_uses_router_model_setting(settings):
    settings.LLM_ROUTER_MODEL_HRID = "default-summarization-model"
    agent = RouterClassifierAgent(system_prompt="x")
    assert agent.configuration.hrid == "default-summarization-model"
    assert agent.get_tools() == []

    settings.LLM_ROUTER_MODEL_HRID = ""
    agent = RouterClassifierAgent(system_prompt="x")
    assert agent.configuration.hrid == settings.LLM_DEFAULT_MODEL_HRID


# --- fallbacks ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_falls_back_to_previous_labels(settings):
    settings.LLM_ROUTER_TIMEOUT_S = 0.01

    async def slow(_messages, _info):
        await asyncio.sleep(1)
        return ModelResponse(parts=[])

    builder = agent_with(FunctionModel(slow))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, latency, version = await run_classify(previous_labels=PREVIOUS)
    assert reason == RoutingReason.FALLBACK
    assert labels == PREVIOUS
    assert latency >= 10
    assert version is None


@pytest.mark.asyncio
async def test_timeout_without_previous_labels_uses_standard_defaults(settings):
    settings.LLM_ROUTER_TIMEOUT_S = 0.01

    async def slow(_messages, _info):
        await asyncio.sleep(1)
        return ModelResponse(parts=[])

    builder = agent_with(FunctionModel(slow))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify()
    assert reason == RoutingReason.FALLBACK
    assert labels == FALLBACK_LABELS
    assert labels.complexity == RoutingTier.STANDARD
    assert labels.domain == RoutingDomain.GENERAL
    assert labels.task == RoutingTask.QA_KNOWLEDGE
    assert labels.confidence == 0.0


@pytest.mark.asyncio
async def test_model_exception_falls_back():
    def boom(_messages, _info):
        raise RuntimeError("provider down")

    builder = agent_with(FunctionModel(boom))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify(previous_labels=PREVIOUS)
    assert reason == RoutingReason.FALLBACK
    assert labels == PREVIOUS


@pytest.mark.asyncio
async def test_invalid_model_output_falls_back():
    """An out-of-range confidence fails validation; after retries the call errors out."""
    output = {"complexity": "simple", "domain": "general", "task": "writing", "confidence": 7}
    builder = agent_with(FunctionModel(model_returns(output)))
    with mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder):
        labels, reason, _, _ = await run_classify()
    assert reason == RoutingReason.FALLBACK
    assert labels == FALLBACK_LABELS


@pytest.mark.asyncio
async def test_agent_build_failure_falls_back():
    with mock.patch(
        "chat.router.classifier.build_classifier_agent", side_effect=ValueError("bad config")
    ):
        labels, reason, _, _ = await run_classify()
    assert reason == RoutingReason.FALLBACK
    assert labels == FALLBACK_LABELS


# --- Langfuse prompt ---------------------------------------------------------


def test_prompt_is_local_default_when_langfuse_disabled(settings):
    settings.LANGFUSE_ENABLED = False
    with mock.patch("langfuse.get_client") as get_client:
        assert get_router_prompt() == (DEFAULT_ROUTER_PROMPT, None)
    get_client.assert_not_called()


def test_prompt_falls_back_when_langfuse_client_raises(settings):
    settings.LANGFUSE_ENABLED = True
    with mock.patch("langfuse.get_client", side_effect=RuntimeError("langfuse down")):
        assert get_router_prompt() == (DEFAULT_ROUTER_PROMPT, None)


def test_prompt_falls_back_when_get_prompt_raises(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.side_effect = ConnectionError("timeout")
    with mock.patch("langfuse.get_client", return_value=client):
        assert get_router_prompt() == (DEFAULT_ROUTER_PROMPT, None)


def test_prompt_fallback_object_has_no_version(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(
        prompt=DEFAULT_ROUTER_PROMPT, version=0, is_fallback=True
    )
    with mock.patch("langfuse.get_client", return_value=client):
        assert get_router_prompt() == (DEFAULT_ROUTER_PROMPT, None)


def test_prompt_from_langfuse_carries_version(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(
        prompt="Langfuse managed prompt", version=7, is_fallback=False
    )
    with mock.patch("langfuse.get_client", return_value=client):
        assert get_router_prompt() == ("Langfuse managed prompt", "7")
    client.get_prompt.assert_called_once_with(
        "router-classifier",
        label="production",
        type="text",
        fallback=DEFAULT_ROUTER_PROMPT,
        max_retries=0,
        fetch_timeout_seconds=2,
    )


@pytest.mark.asyncio
async def test_classify_records_langfuse_prompt_version(settings):
    settings.LANGFUSE_ENABLED = True
    output = {"complexity": "simple", "domain": "general", "task": "writing", "confidence": 0.9}
    recorded = []
    builder = agent_with(FunctionModel(model_returns(output)), recorded)
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(
        prompt="Langfuse managed prompt", version=3, is_fallback=False
    )
    with (
        mock.patch("langfuse.get_client", return_value=client),
        mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder),
    ):
        _, reason, _, version = await run_classify()
    assert reason == RoutingReason.CLASSIFIED
    assert version == "3"
    assert recorded == ["Langfuse managed prompt"]


@pytest.mark.asyncio
async def test_classify_survives_langfuse_outage(settings):
    settings.LANGFUSE_ENABLED = True
    output = {"complexity": "simple", "domain": "general", "task": "writing", "confidence": 0.9}
    recorded = []
    builder = agent_with(FunctionModel(model_returns(output)), recorded)
    with (
        mock.patch("langfuse.get_client", side_effect=RuntimeError("down")),
        mock.patch("chat.router.classifier.build_classifier_agent", side_effect=builder),
    ):
        labels, reason, _, version = await run_classify()
    assert reason == RoutingReason.CLASSIFIED
    assert labels.complexity == RoutingTier.SIMPLE
    assert version is None
    assert recorded == [DEFAULT_ROUTER_PROMPT]


# --- shared objects ----------------------------------------------------------


def test_routing_labels_validate_confidence_range():
    with pytest.raises(ValueError):
        RoutingLabels(
            complexity=RoutingTier.SIMPLE,
            domain=RoutingDomain.GENERAL,
            task=RoutingTask.WRITING,
            confidence=1.5,
        )


def test_routing_decision_serializes_to_plain_values():
    decision = RoutingDecision(
        tier=RoutingTier.STANDARD,
        tier_source="router",
        model_hrid="default-model",
        labels=PREVIOUS,
        reason=RoutingReason.CLASSIFIED,
        router_would_pick=RoutingTier.COMPLEX,
        router_confidence=0.8,
        router_latency_ms=120,
        router_prompt_version="3",
        router_model_hrid="default-model",
    )
    dumped = decision.model_dump(mode="json")
    assert dumped["tier"] == "standard"
    assert dumped["tier_source"] == "router"
    assert dumped["reasoning_effort"] is None
    assert dumped["labels"]["domain"] == "legal"
    assert dumped["reason"] == "classified"
