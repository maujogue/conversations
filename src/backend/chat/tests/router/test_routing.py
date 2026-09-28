"""Tests for the routing step and RoutingTierSettings (docs/llm-router-spec.md sections 5, 9)."""

# pylint: disable=missing-function-docstring, redefined-outer-name, unused-argument

from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError

import pytest
from asgiref.sync import sync_to_async

from chat.ai_sdk_types import FileUIPart, TextUIPart, UIMessage
from chat.enums import (
    ReasoningEffort,
    RoutingDomain,
    RoutingReason,
    RoutingTask,
    RoutingTier,
    TierSource,
)
from chat.factories import ChatConversationFactory
from chat.llm_configuration import LLModel, LLMProvider
from chat.model_health import set_model_health
from chat.models import RoutingTierSettings
from chat.router import RoutingLabels, route_pinned_turn, route_turn
from chat.router.routing import (
    constraint_fallback_key,
    last_answer_excerpt,
    last_routing_payload,
    previous_labels_from,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("clear_cache"),
]


def _llm(hrid, **overrides) -> LLModel:
    values = {
        "hrid": hrid,
        "model_name": f"{hrid}-name",
        "human_readable_name": hrid,
        "is_active": True,
        "system_prompt": "You are a helpful assistant.",
        "tools": [],
        "provider": LLMProvider(hrid="albert", base_url="https://albert.example/v1", api_key="k"),
    }
    values.update(overrides)
    return LLModel(**values)


@pytest.fixture(autouse=True)
def tier_configuration(settings):
    """Three tiers with alternatives, a text-only reasoning model on tier 3."""
    settings.LLM_CONFIGURATIONS = {
        "default-model": _llm("default-model", supports_image=True, web_search="chat.x.y"),
        "small": _llm("small", supports_image=False, max_token_context=1000),
        "small-vision": _llm("small-vision", supports_image=True),
        "medium": _llm("medium", supports_image=True, web_search="chat.x.y"),
        "medium-alt": _llm("medium-alt", supports_image=True),
        "reasoner": _llm("reasoner", supports_image=False, reasoning_control="levels"),
        "reasoner-vision": _llm("reasoner-vision", supports_image=True),
        "thinker": _llm("thinker", supports_image=False, reasoning_control="toggle"),
        "summarizer": _llm("summarizer", role="utility"),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "default-model"
    settings.LLM_FALLBACK_MODEL_HRID_1 = ""
    settings.LLM_FALLBACK_MODEL_HRID_2 = ""
    settings.LLM_TIER_SIMPLE_MODEL_HRID = ""
    settings.LLM_TIER_STANDARD_MODEL_HRID = ""
    settings.LLM_TIER_COMPLEX_MODEL_HRID = ""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tier_settings():
    tier_settings = RoutingTierSettings.get_solo()
    tier_settings.simple_model_hrid = "small"
    tier_settings.simple_alternatives = ["small-vision"]
    tier_settings.standard_model_hrid = "medium"
    tier_settings.standard_alternatives = ["medium-alt"]
    tier_settings.complex_model_hrid = "reasoner"
    tier_settings.complex_alternatives = ["reasoner-vision"]
    tier_settings.save()
    return tier_settings


def labels(complexity=RoutingTier.STANDARD, confidence=0.8, task=RoutingTask.WRITING):
    return RoutingLabels(
        complexity=complexity, domain=RoutingDomain.GENERAL, task=task, confidence=confidence
    )


def classify_returning(labels_, reason=RoutingReason.CLASSIFIED, latency_ms=120, version="3"):
    async def fake_classify(*_args, **_kwargs):
        return labels_, reason.value, latency_ms, version

    return mock.patch("chat.router.routing.classify", side_effect=fake_classify)


def message(text="Rédige une note de trois pages sur la réforme", with_image=False):
    parts = [TextUIPart(type="text", text=text)]
    if with_image:
        parts.append(FileUIPart(type="file", mediaType="image/png", url="data:image/png;base64,AA"))
    return UIMessage(id="u1", role="user", parts=parts)


async def make_conversation(**kwargs):
    """Factories touch the database: run them off the event loop."""
    return await sync_to_async(ChatConversationFactory)(**kwargs)


async def save(instance):
    await sync_to_async(instance.save)()


async def route(conversation, msg=None, **overrides):
    kwargs = {
        "conversation": conversation,
        "user": conversation.owner,
        "message": msg or message(),
        "force_web_search": False,
        "has_attachments": False,
        "has_project_context": False,
        "requested_model_hrid": None,
        "previous_labels": None,
        "previous_answer_excerpt": None,
    }
    kwargs.update(overrides)
    return await route_turn(**kwargs)


# --- RoutingTierSettings -----------------------------------------------------


def test_model_for_falls_back_to_settings_then_default(settings):
    tier_settings = RoutingTierSettings.get_solo()
    assert tier_settings.model_for(RoutingTier.SIMPLE) == "default-model"
    settings.LLM_TIER_SIMPLE_MODEL_HRID = "small"
    assert tier_settings.model_for(RoutingTier.SIMPLE) == "small"
    tier_settings.simple_model_hrid = "small-vision"
    assert tier_settings.model_for("simple") == "small-vision"


def test_alternatives_exclude_the_tier_model_and_all_models_dedupes(tier_settings):
    tier_settings.standard_alternatives = ["medium", "medium-alt", "medium-alt", ""]
    assert tier_settings.alternatives_for(RoutingTier.STANDARD) == ["medium-alt"]
    assert tier_settings.all_models_for(RoutingTier.STANDARD) == ["medium", "medium-alt"]


def test_clean_rejects_unknown_and_utility_models(tier_settings):
    tier_settings.simple_model_hrid = "summarizer"
    tier_settings.complex_alternatives = ["nope"]
    tier_settings.router_model_hrid = "ghost"
    with pytest.raises(ValidationError) as exc:
        tier_settings.clean()
    assert set(exc.value.message_dict) == {
        "simple_model_hrid",
        "complex_alternatives",
        "router_model_hrid",
    }


def test_clean_accepts_blank_and_chat_models(tier_settings):
    tier_settings.router_model_hrid = "summarizer"  # any configured model may classify
    tier_settings.clean()


# --- thresholds and tier choice ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "complexity,confidence,expected",
    [
        (RoutingTier.SIMPLE, 0.95, RoutingTier.SIMPLE),
        (RoutingTier.SIMPLE, 0.5, RoutingTier.STANDARD),
        (RoutingTier.COMPLEX, 0.7, RoutingTier.COMPLEX),
        (RoutingTier.COMPLEX, 0.69, RoutingTier.STANDARD),
        (RoutingTier.STANDARD, 0.1, RoutingTier.STANDARD),
    ],
)
async def test_confidence_threshold_gates_tiers_one_and_three(
    tier_settings, complexity, confidence, expected
):
    conversation = await make_conversation()
    with classify_returning(labels(complexity, confidence)):
        decision = await route(conversation)
    assert decision.tier == expected
    assert decision.tier_source == TierSource.ROUTER
    assert decision.reason == RoutingReason.CLASSIFIED
    assert decision.router_would_pick == expected
    assert decision.router_confidence == confidence
    assert decision.router_latency_ms == 120
    assert decision.router_prompt_version == "3"
    assert decision.model_hrid == tier_settings.model_for(expected)


@pytest.mark.asyncio
async def test_admin_threshold_is_used(tier_settings):
    tier_settings.confidence_threshold = 0.95
    await save(tier_settings)
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.9)):
        decision = await route(conversation)
    assert decision.tier == RoutingTier.STANDARD


@pytest.mark.asyncio
async def test_classifier_fallback_reason_is_kept(tier_settings):
    conversation = await make_conversation(model_hrid="medium")
    with classify_returning(labels(RoutingTier.STANDARD, 0.0), reason=RoutingReason.FALLBACK):
        decision = await route(conversation)
    assert decision.reason == RoutingReason.FALLBACK
    assert decision.tier == RoutingTier.STANDARD
    assert decision.previous_model_hrid == "medium"
    assert decision.changed is False


@pytest.mark.asyncio
async def test_no_op_when_nothing_is_configured(settings):
    """Blank tiers all resolve to the default model: the router changes nothing."""
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.99)):
        decision = await route(conversation)
    assert decision.tier == RoutingTier.SIMPLE
    assert decision.model_hrid == settings.LLM_DEFAULT_MODEL_HRID


# --- pinned tier ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_tier_bypasses_complexity_but_keeps_tags(tier_settings):
    conversation = await make_conversation(pinned_tier=RoutingTier.COMPLEX)
    with classify_returning(labels(RoutingTier.SIMPLE, 0.99, task=RoutingTask.TRANSLATION)):
        decision = await route(conversation)
    assert decision.tier == RoutingTier.COMPLEX
    assert decision.tier_source == TierSource.USER
    assert decision.reason == RoutingReason.USER_PINNED
    assert decision.router_would_pick == RoutingTier.SIMPLE
    assert decision.labels.task == RoutingTask.TRANSLATION
    assert decision.model_hrid == "reasoner"
    # Pinned "Raisonnement" always runs at high effort (spec 3.2).
    assert decision.reasoning_effort == ReasoningEffort.HIGH


# --- pinned tier without the classifier (router flag off) ------------------------


async def route_pinned(conversation, msg=None, force_web_search=False):
    return await route_pinned_turn(
        conversation=conversation,
        message=msg or message(),
        force_web_search=force_web_search,
    )


@pytest.mark.asyncio
async def test_pinned_turn_runs_the_tier_model_without_classifying(tier_settings):
    """The selector is offered to everyone, so a pin is served with no router call."""
    conversation = await make_conversation(pinned_tier=RoutingTier.COMPLEX)
    with mock.patch("chat.router.routing.classify") as classify:
        decision = await route_pinned(conversation)
    classify.assert_not_called()
    assert decision.tier == RoutingTier.COMPLEX
    assert decision.tier_source == TierSource.USER
    assert decision.reason == RoutingReason.USER_PINNED
    assert decision.model_hrid == "reasoner"
    # Pinned "Raisonnement" always runs at high effort (spec 3.2).
    assert decision.reasoning_effort == ReasoningEffort.HIGH
    # Nothing classified this turn: no labels, and no "Auto would have picked".
    assert decision.labels is None
    assert decision.router_would_pick is None


@pytest.mark.asyncio
async def test_pinned_turn_still_walks_the_constraints(tier_settings):
    """Constraints are capabilities, not preferences: they apply to a pin too."""
    conversation = await make_conversation(pinned_tier=RoutingTier.COMPLEX)
    decision = await route_pinned(conversation, message(with_image=True))
    assert decision.model_hrid == "reasoner-vision"


# --- constraints -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_walks_to_the_vision_alternative_of_the_same_tier(tier_settings):
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.9)):
        decision = await route(conversation, message(with_image=True))
    assert decision.tier == RoutingTier.SIMPLE
    assert decision.model_hrid == "small-vision"
    assert decision.tier_source == TierSource.ROUTER  # same tier: no bump


@pytest.mark.asyncio
async def test_image_on_tier_three_lands_on_the_vision_alternative(tier_settings):
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.COMPLEX, 0.95, task=RoutingTask.REASONING)):
        decision = await route(conversation, message(with_image=True))
    assert decision.model_hrid == "reasoner-vision"
    assert decision.reasoning_effort is None  # dense model: no effort control


@pytest.mark.asyncio
async def test_constraint_walks_up_to_the_next_tier(tier_settings):
    tier_settings.simple_alternatives = []
    await save(tier_settings)
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.9)):
        decision = await route(conversation, message(with_image=True))
    assert decision.tier == RoutingTier.STANDARD
    assert decision.model_hrid == "medium"
    assert decision.tier_source == TierSource.CONSTRAINT
    assert decision.reason == RoutingReason.CONSTRAINT
    assert decision.router_would_pick == RoutingTier.SIMPLE


@pytest.mark.asyncio
async def test_forced_web_search_needs_a_model_with_web_search(tier_settings):
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.9)):
        decision = await route(conversation, force_web_search=True)
    assert decision.model_hrid == "medium"
    assert decision.tier == RoutingTier.STANDARD
    assert decision.reason == RoutingReason.CONSTRAINT


@pytest.mark.asyncio
async def test_context_length_walks_up(tier_settings):
    conversation = await make_conversation(pydantic_messages=[{"text": "x" * 8000}])
    with classify_returning(labels(RoutingTier.SIMPLE, 0.9)):
        decision = await route(conversation)
    # "small" has a 1000 token context; the history is about 2000 tokens.
    assert decision.model_hrid == "small-vision"
    assert decision.tier == RoutingTier.SIMPLE


@pytest.mark.asyncio
async def test_constraint_fallback_uses_default_and_counts(tier_settings, settings):
    """No tier model can read images when web search is also forced: default model."""
    for hrid in ("small-vision", "medium", "medium-alt", "reasoner-vision"):
        settings.LLM_CONFIGURATIONS[hrid].web_search = None
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.STANDARD, 0.9)):
        decision = await route(conversation, message(with_image=True), force_web_search=True)
    assert decision.model_hrid == "default-model"
    assert decision.reason == RoutingReason.CONSTRAINT_FALLBACK
    assert decision.tier_source == TierSource.CONSTRAINT
    assert decision.tier == RoutingTier.STANDARD
    assert cache.get(constraint_fallback_key("standard")) == 1

    with classify_returning(labels(RoutingTier.STANDARD, 0.9)):
        await route(conversation, message(with_image=True), force_web_search=True)
    assert cache.get(constraint_fallback_key("standard")) == 2


# --- health cascade ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_red_tier_model_cascades_to_its_alternative(tier_settings):
    set_model_health("albert", "medium-name", "red")
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.STANDARD, 0.9)):
        decision = await route(conversation)
    assert decision.model_hrid == "medium-alt"
    assert decision.tier == RoutingTier.STANDARD


@pytest.mark.asyncio
async def test_red_tier_model_then_settings_fallback(tier_settings, settings):
    settings.LLM_FALLBACK_MODEL_HRID_1 = "default-model"
    tier_settings.standard_alternatives = []
    await save(tier_settings)
    set_model_health("albert", "medium-name", "red")
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.STANDARD, 0.9)):
        decision = await route(conversation)
    assert decision.model_hrid == "default-model"


@pytest.mark.asyncio
async def test_everything_red_keeps_the_tier_model(tier_settings):
    set_model_health("albert", "medium-name", "red")
    set_model_health("albert", "medium-alt-name", "red")
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.STANDARD, 0.9)):
        decision = await route(conversation)
    assert decision.model_hrid == "medium"


# --- reasoning effort ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confidence,task,expected",
    [
        (0.75, RoutingTask.WRITING, ReasoningEffort.MEDIUM),
        (0.9, RoutingTask.WRITING, ReasoningEffort.HIGH),
        (0.75, RoutingTask.REASONING, ReasoningEffort.HIGH),
        (0.75, RoutingTask.DATA_ANALYSIS, ReasoningEffort.HIGH),
        (0.75, RoutingTask.CODING, ReasoningEffort.HIGH),
    ],
)
async def test_effort_on_tier_three_levels_model(tier_settings, confidence, task, expected):
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.COMPLEX, confidence, task=task)):
        decision = await route(conversation)
    assert decision.model_hrid == "reasoner"
    assert decision.reasoning_effort == expected


@pytest.mark.asyncio
async def test_effort_threshold_is_configurable(tier_settings):
    tier_settings.high_effort_threshold = 0.8
    await save(tier_settings)
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.COMPLEX, 0.85)):
        decision = await route(conversation)
    assert decision.reasoning_effort == ReasoningEffort.HIGH


@pytest.mark.asyncio
async def test_toggle_model_gets_no_effort(tier_settings):
    tier_settings.complex_model_hrid = "thinker"
    await save(tier_settings)
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.COMPLEX, 0.99, task=RoutingTask.REASONING)):
        decision = await route(conversation)
    assert decision.model_hrid == "thinker"
    assert decision.reasoning_effort is None


@pytest.mark.asyncio
async def test_no_effort_below_tier_three(tier_settings):
    tier_settings.standard_model_hrid = "reasoner"
    await save(tier_settings)
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.STANDARD, 0.99, task=RoutingTask.CODING)):
        decision = await route(conversation)
    assert decision.model_hrid == "reasoner"
    assert decision.reasoning_effort is None


# --- staff picker ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requested_model_is_used_as_is(tier_settings):
    conversation = await make_conversation()
    with classify_returning(labels(RoutingTier.SIMPLE, 0.99)):
        decision = await route(conversation, requested_model_hrid="reasoner-vision")
    assert decision.model_hrid == "reasoner-vision"
    assert decision.tier == RoutingTier.COMPLEX
    assert decision.tier_source == TierSource.USER
    assert decision.reason == RoutingReason.USER_PINNED
    assert decision.router_would_pick == RoutingTier.SIMPLE


# --- previous turn hints -----------------------------------------------------------------


def test_last_routing_round_trip():
    conversation = ChatConversationFactory()
    assert previous_labels_from(conversation) is None

    class _Decision:  # minimal stand-in for last_routing_payload
        tier = RoutingTier.COMPLEX
        model_hrid = "reasoner"
        labels = labels(RoutingTier.COMPLEX, 0.8, task=RoutingTask.CODING)

    conversation.last_routing = last_routing_payload(_Decision())
    assert conversation.last_routing["tier"] == "complex"
    assert conversation.last_routing["model_hrid"] == "reasoner"
    assert previous_labels_from(conversation) == _Decision.labels

    conversation.last_routing = {"labels": {"complexity": "nope"}}
    assert previous_labels_from(conversation) is None


def test_last_answer_excerpt_takes_the_tail_of_the_last_assistant_message():
    conversation = ChatConversationFactory(
        ui_messages=[
            {"role": "assistant", "parts": [{"type": "text", "text": "first"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "x" * 1000}]},
            {"role": "user", "parts": [{"type": "text", "text": "question"}]},
        ]
    )
    excerpt = last_answer_excerpt(conversation)
    assert excerpt == "x" * 800
    assert last_answer_excerpt(ChatConversationFactory(ui_messages=[])) is None


@pytest.mark.asyncio
async def test_previous_labels_and_excerpt_reach_the_classifier(tier_settings):
    conversation = await make_conversation()
    previous = labels(RoutingTier.COMPLEX, 0.9)
    with classify_returning(labels()) as classify:
        await route(
            conversation,
            previous_labels=previous,
            previous_answer_excerpt="the end",
            has_attachments=True,
            has_project_context=True,
            force_web_search=False,
        )
    classify.assert_called_once()
    args = classify.call_args.args
    assert args[1] == previous
    assert args[2] == "the end"
    assert args[3] is True and args[4] is True and args[5] is False
