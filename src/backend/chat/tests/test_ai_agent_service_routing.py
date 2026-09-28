"""Routing decision plumbing in AIAgentService (docs/llm-router-spec.md sections 3.2, 5.5, 7.1, 10)."""

# pylint: disable=protected-access, redefined-outer-name, missing-function-docstring
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import RunUsage
from pydantic_ai.messages import (
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from chat.clients.pydantic_ai import AIAgentService
from chat.clients.schema import ImagePostRunActions, StreamingState, TurnMetrics
from chat.enums import (
    ReasoningEffort,
    RoutingDomain,
    RoutingReason,
    RoutingTask,
    RoutingTier,
    TierSource,
)
from chat.llm_configuration import LLModel
from chat.router import RoutingDecision, RoutingLabels
from chat.vercel_ai_sdk.core import events_v4, events_v5
from chat.vercel_ai_sdk.encoder import CURRENT_EVENT_ENCODER_VERSION, EventEncoder
from chat.vercel_ai_sdk.encoder.v4_to_v5 import V4ToV5Translator


def _fake_sync_to_async(fn):
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def _llm(**overrides) -> LLModel:
    values = {
        "hrid": "m",
        "model_name": "test:model",
        "human_readable_name": "M",
        "is_active": True,
        "system_prompt": "hi",
        "tools": [],
    }
    values.update(overrides)
    return LLModel(**values)


DECISION = RoutingDecision(
    tier=RoutingTier.COMPLEX,
    tier_source=TierSource.ROUTER,
    model_hrid="m",
    reasoning_effort=ReasoningEffort.HIGH,
    labels=RoutingLabels(
        complexity=RoutingTier.COMPLEX,
        domain=RoutingDomain.LEGAL,
        task=RoutingTask.REASONING,
        confidence=0.93,
    ),
    reason=RoutingReason.CLASSIFIED,
    router_would_pick=RoutingTier.COMPLEX,
    router_confidence=0.93,
    router_latency_ms=210,
    router_prompt_version="4",
    previous_model_hrid="other",
)


@pytest.fixture(name="conversation")
def conversation_fixture():
    conv = MagicMock()
    conv.pk = 42
    conv.messages = []
    conv.pydantic_messages = []
    conv.agent_usage = {}
    conv.title_set_by_user_at = None
    return conv


@pytest.fixture(name="service")
def service_fixture(conversation):
    """AIAgentService without __init__, with a routed reasoning model."""
    s = object.__new__(AIAgentService)
    s.conversation = conversation
    s._arena_comparison = None
    s._arena_started_at = None
    s._arena_first_token_at = None
    s._routing_decision = DECISION
    s._turn_metrics = TurnMetrics()
    s.user = SimpleNamespace(pk=1, sub="sub-1", email="agent@example.gouv.fr")
    s.model_configuration = _llm(
        reasoning_control="levels", total_params_b=117, active_params_b=5.1
    )
    s.conversation_agent = SimpleNamespace(configuration=s.model_configuration)
    s.event_encoder = EventEncoder(CURRENT_EVENT_ENCODER_VERSION)
    s._langfuse_available = False
    s._last_stop_check = 0
    s._pre_stream_events = []
    return s


# --- data part -----------------------------------------------------------------------------


def test_routing_data_part_and_its_v5_translation(service):
    part = service._routing_data_part()
    assert part == {
        "type": "routing",
        "tier": "complex",
        "tier_label": "router.tier.complex",
        "tier_source": "router",
        "changed": True,
        "reasoning": True,
    }
    translated = V4ToV5Translator().translate(events_v4.DataPart(data=[part]))
    assert len(translated) == 1
    assert isinstance(translated[0], events_v5.DataPart)
    assert translated[0].type == "data-routing"
    assert translated[0].data == part
    assert translated[0].transient is True


def test_routing_data_part_flags(service):
    service._routing_decision = DECISION.model_copy(update={"previous_model_hrid": "m"})
    service.model_configuration = _llm(reasoning_control="none")
    part = service._routing_data_part()
    assert part["changed"] is False
    assert part["reasoning"] is False

    service._routing_decision = None
    assert service._routing_data_part() is None


@pytest.mark.asyncio
async def test_run_agent_emits_the_routing_part_before_the_model_runs(service):
    """The routing part comes right after the pre-stream events, before any token."""
    service._pre_stream_events = [{"type": "images_skipped"}]
    prepared = ("prompt", [], [], ImagePostRunActions(), {}, [], False)

    async def stop_here(*_args, **_kwargs):
        """Stop the run right after the input documents phase."""
        yield SimpleNamespace(success=False, has_documents=False)

    with (
        patch.object(service, "_prepare_agent_run", AsyncMock(return_value=prepared)),
        patch.object(service, "_handle_input_documents", side_effect=stop_here),
        patch("chat.clients.pydantic_ai.DocumentParsingResult", SimpleNamespace),
    ):
        service._is_document_upload_enabled = False
        events = [
            event
            async for event in service._run_agent(
                [SimpleNamespace(role="user")], force_web_search=False
            )
        ]
    data_types = [
        event.data[0]["type"] for event in events if isinstance(event, events_v4.DataPart)
    ]
    assert data_types == ["images_skipped", "routing"]


# --- langfuse tags and metadata -------------------------------------------------------------


def test_trace_tags_and_metadata(service):
    assert service._routing_trace_tags() == [
        "tier:complex",
        "tier_source:router",
        "routed:classified",
        "domain:legal",
        "task:reasoning",
    ]
    assert service._routing_trace_metadata() == {
        "tier": "complex",
        "tier_source": "router",
        "domain": "legal",
        "task": "reasoning",
        "router_reason": "classified",
        "router_confidence": "0.93",
    }
    service._routing_decision = None
    assert service._routing_trace_tags() == []
    assert service._routing_trace_metadata() == {}


def test_arena_trace_tags_and_metadata(service):
    """An arena side carries the labels a Langfuse reader needs to slice its vote."""
    assert service._arena_trace_tags() == []
    assert service._arena_trace_metadata() == {}

    service._arena_comparison = SimpleNamespace(
        experiment_id="11111111-2222-3333-4444-555555555555", origin="draw"
    )
    service._arena_role = "challenger"
    service.model_hrid = "ministral-3-8b"

    assert service._arena_trace_tags() == [
        "arena:challenger",
        "arena_experiment:11111111-2222-3333-4444-555555555555",
        "arena_origin:draw",
        "model:ministral-3-8b",
    ]
    assert service._arena_trace_metadata() == {
        "arena": "challenger",
        "arena_experiment": "11111111-2222-3333-4444-555555555555",
        "arena_origin": "draw",
        "model": "ministral-3-8b",
    }

    # A side that never got a role is not an arena trace, whatever the comparison says.
    service._arena_role = None
    assert service._arena_trace_tags() == []
    assert service._arena_trace_metadata() == {}


@pytest.mark.asyncio
async def test_arena_tags_are_appended_to_the_routing_tags(service):
    service._langfuse_available = True
    service._arena_comparison = SimpleNamespace(experiment_id="exp-1", origin="manual")
    service._arena_role = "champion"
    service.model_hrid = "mistral-small-3-2"

    @contextmanager
    def fake_observation(**_kwargs):
        yield MagicMock()

    langfuse_client = MagicMock()
    langfuse_client.get_current_trace_id.return_value = "trace123"
    langfuse_client.start_as_current_observation = fake_observation

    async def no_events(*_args, **_kwargs):
        if False:  # pragma: no cover - makes this an async generator
            yield None

    with (
        patch("chat.clients.pydantic_ai.propagate_attributes") as propagate,
        patch("chat.clients.pydantic_ai.get_client", return_value=langfuse_client),
        patch.object(service, "_run_agent", side_effect=no_events),
    ):
        _ = [chunk async for chunk in service._stream_content([], force_web_search=False)]

    kwargs = propagate.call_args.kwargs
    assert kwargs["tags"][-4:] == [
        "arena:champion",
        "arena_experiment:exp-1",
        "arena_origin:manual",
        "model:mistral-small-3-2",
    ]
    assert kwargs["tags"][0] == "tier:complex"
    assert kwargs["metadata"]["arena"] == "champion"
    assert kwargs["metadata"]["model"] == "mistral-small-3-2"


@pytest.mark.asyncio
async def test_tags_are_passed_to_propagate_attributes(service):
    service._langfuse_available = True

    @contextmanager
    def fake_observation(**_kwargs):
        yield MagicMock()

    langfuse_client = MagicMock()
    langfuse_client.get_current_trace_id.return_value = "trace123"
    langfuse_client.start_as_current_observation = fake_observation

    async def no_events(*_args, **_kwargs):
        if False:  # pragma: no cover - makes this an async generator
            yield None

    with (
        patch("chat.clients.pydantic_ai.propagate_attributes") as propagate,
        patch("chat.clients.pydantic_ai.get_client", return_value=langfuse_client),
        patch.object(service, "_run_agent", side_effect=no_events),
    ):
        _ = [chunk async for chunk in service._stream_content([], force_web_search=False)]

    propagate.assert_called_once()
    kwargs = propagate.call_args.kwargs
    assert kwargs["tags"] == [
        "tier:complex",
        "tier_source:router",
        "routed:classified",
        "domain:legal",
        "task:reasoning",
    ]
    assert kwargs["metadata"]["user_fqdn"] == "example.gouv.fr"
    assert kwargs["metadata"]["tier"] == "complex"
    assert kwargs["metadata"]["router_confidence"] == "0.93"


@pytest.mark.asyncio
async def test_a_user_without_an_email_still_gets_an_answer(service):
    """Device users and some OIDC providers have no email: tracing must cope."""
    service._langfuse_available = True
    service.user.email = None

    @contextmanager
    def fake_observation(**_kwargs):
        yield MagicMock()

    langfuse_client = MagicMock()
    langfuse_client.get_current_trace_id.return_value = "trace123"
    langfuse_client.start_as_current_observation = fake_observation

    async def no_events(*_args, **_kwargs):
        if False:  # pragma: no cover - makes this an async generator
            yield None

    with (
        patch("chat.clients.pydantic_ai.propagate_attributes") as propagate,
        patch("chat.clients.pydantic_ai.get_client", return_value=langfuse_client),
        patch.object(service, "_run_agent", side_effect=no_events),
    ):
        _ = [chunk async for chunk in service._stream_content([], force_web_search=False)]

    metadata = propagate.call_args.kwargs["metadata"]
    assert "user_fqdn" not in metadata
    assert metadata["tier"] == "complex"


# --- reasoning effort override ---------------------------------------------------------------


def test_model_settings_override_only_for_levels(service):
    override = service._model_settings_override()
    assert override == {"openai_reasoning_effort": "high"}

    service.model_configuration = _llm(reasoning_control="toggle")
    assert service._model_settings_override() is None

    service.model_configuration = _llm(reasoning_control="levels")
    service._routing_decision = DECISION.model_copy(update={"reasoning_effort": None})
    assert service._model_settings_override() is None


# --- persisted metadata ------------------------------------------------------------------------


def test_routing_metadata_persisted_on_the_assistant_message(conversation, service):
    service._turn_metrics = TurnMetrics(
        reasoning_tokens=350, reasoning_seconds=12.4, co2_source="estimated_reasoning"
    )
    service._prepare_update_conversation(
        final_output=[ModelResponse(parts=[TextPart(content="Hello")], kind="response")],
        usage={"promptTokens": 10, "completionTokens": 5, "co2_impact": 2e-6},
        model_response_message_id="msg-1",
    )
    assistant = conversation.messages[-1]
    assert assistant.metadata == {
        "tier": "complex",
        "tier_source": "router",
        "domain": "legal",
        "task": "reasoning",
        "router_reason": "classified",
        "router_confidence": 0.93,
        "router_would_pick": "complex",
        "reasoning_effort": "high",
        "router_latency_ms": 210,
        "router_prompt_version": "4",
        "reasoning_tokens": 350,
        "reasoning_seconds": 12.4,
        "co2_source": "estimated_reasoning",
        "co2_impact": pytest.approx(2e-6),
    }


def test_no_routing_metadata_without_a_decision(conversation, service):
    service._routing_decision = None
    service._prepare_update_conversation(
        final_output=[ModelResponse(parts=[TextPart(content="Hello")], kind="response")],
        usage={"promptTokens": 10, "completionTokens": 5, "co2_impact": 0},
        model_response_message_id="msg-1",
    )
    assert not conversation.messages[-1].metadata


# --- reasoning seconds --------------------------------------------------------------------------


def _fake_node(events):
    @asynccontextmanager
    async def stream(_run_ctx):
        async def gen():
            for event in events:
                yield event

        yield gen()

    return SimpleNamespace(stream=stream)


@pytest.mark.asyncio
async def test_reasoning_seconds_measured_from_first_thinking_to_first_text(service):
    node = _fake_node(
        [
            PartStartEvent(index=0, part=ThinkingPart(content="Let me")),
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" think")),
            PartStartEvent(index=1, part=TextPart(content="Answer")),
            PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="!")),
        ]
    )
    state = StreamingState()
    clock = iter([100.0, 105.0, 123.4, 130.0, 999.0])
    with (
        patch.object(service, "_agent_stop_streaming", AsyncMock()),
        patch("chat.clients.pydantic_ai.time.monotonic", side_effect=lambda: next(clock)),
    ):
        events = [e async for e in service._handle_streaming_response(node, None, state)]

    assert [type(e).__name__ for e in events] == [
        "ReasoningPart",
        "ReasoningPart",
        "TextPart",
        "TextPart",
    ]
    assert state.first_thinking_at == 100.0
    assert state.first_text_at == 123.4
    assert state.reasoning_seconds(now=500.0) == 23.4


def test_reasoning_seconds_without_thinking_is_none():
    state = StreamingState()
    state.note_text(10.0)
    assert state.reasoning_seconds(now=20.0) is None
    only_thinking = StreamingState()
    only_thinking.note_thinking(10.0)
    assert only_thinking.reasoning_seconds(now=20.5) == 10.5


# --- footprint ---------------------------------------------------------------------------------------


def test_resolve_turn_footprint_estimates_reasoning_models(service):
    service._arena_started_at = 0.0
    state = StreamingState()
    state.note_thinking(1.0)
    state.note_text(9.0)
    usage = RunUsage(output_tokens=20, details={"reasoning_chars": 4000, "co2_impact_factor_20": 5})
    with patch("chat.clients.pydantic_ai.time.monotonic", return_value=12.0):
        co2 = service._resolve_turn_footprint(usage, state)
    assert co2 > 0
    assert service._turn_metrics.reasoning_tokens == 1000
    assert service._turn_metrics.reasoning_seconds == 8.0
    assert service._turn_metrics.co2_source == "estimated_reasoning"


def test_resolve_turn_footprint_keeps_the_provider_figure_for_plain_models(service):
    service.model_configuration = _llm(reasoning_control="none")
    usage = RunUsage(output_tokens=20, details={"co2_impact_factor_20": 3 * 10**14})
    co2 = service._resolve_turn_footprint(usage, StreamingState())
    assert co2 == pytest.approx(3e-6)
    assert service._turn_metrics.co2_source == "provider"
    assert service._turn_metrics.reasoning_tokens == 0
    assert service._turn_metrics.reasoning_seconds is None


# --- cooldown and streamed annotation --------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_feeds_reasoning_tokens_to_the_cooldown_and_streams_the_annotation(
    service,
):
    service._turn_metrics = TurnMetrics(
        reasoning_tokens=1000, reasoning_seconds=8.0, co2_source="estimated_reasoning"
    )
    usage = {"promptTokens": 10, "completionTokens": 5, "co2_impact": 1e-6}
    state = StreamingState(model_response_message_id="test-msg-id")

    with (
        patch.object(service, "_agent_stop_streaming", new=AsyncMock()),
        patch.object(service, "_prepare_update_conversation"),
        patch.object(service, "_save_completed_conversation", return_value=True),
        patch.object(service, "_generate_title_if_needed", new=AsyncMock(return_value=None)),
        patch("chat.clients.pydantic_ai.sync_to_async", side_effect=_fake_sync_to_async),
        patch("chat.clients.pydantic_ai.record_and_compute_cooldown", return_value=0) as cooldown,
    ):
        events = [
            event
            async for event in service._finalize_conversation(
                new_messages=[],
                run_output="Hello",
                usage=usage,
                state=state,
                image_actions=ImagePostRunActions(),
            )
        ]

    cooldown.assert_called_once_with(1, service.conversation_agent.configuration, 10 + 5 + 1000)

    annotations = [e for e in events if isinstance(e, events_v4.MessageAnnotationPart)]
    assert len(annotations) == 1
    annotation = annotations[0].annotations[0]
    assert annotation["tier"] == "complex"
    assert annotation["reasoning_effort"] == "high"
    assert annotation["reasoning_tokens"] == 1000
    assert annotation["reasoning_seconds"] == 8.0
    assert annotation["co2_source"] == "estimated_reasoning"
    assert annotation["co2_impact"] == pytest.approx(1e-6)

    # The annotation lands in the v5 finish metadata, like co2_impact does.
    translator = V4ToV5Translator()
    for event in events:
        translated = translator.translate(event)
    finish = translated[-1]
    assert isinstance(finish, events_v5.FinishMessagePart)
    assert finish.messageMetadata["tier"] == "complex"
    assert finish.messageMetadata["reasoning_seconds"] == 8.0
