"""Tests for the `refresh_tier_energy` command and the Wh-per-tier computation."""

# pylint: disable=redefined-outer-name, unused-argument

from datetime import timedelta

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

import pytest

from chat import models, tier_energy
from chat.ai_sdk_types import TextUIPart, UIMessage
from chat.enums import RoutingTier
from chat.factories import ArenaComparisonFactory, ChatConversationFactory
from chat.footprint import wh_from_co2_kg
from chat.llm_configuration import LLModel, LLMProvider
from chat.views.llm_config import energy_ratio

pytestmark = pytest.mark.django_db

CO2_PER_ANSWER = 0.0001  # kgCO2eq


def _llm(hrid: str, total_params_b=None) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        system_prompt="You are a helpful assistant.",
        tools=[],
        total_params_b=total_params_b,
        provider=LLMProvider(
            hrid="albert", base_url="https://www.external-ai-service.com/", api_key="k"
        ),
    )


@pytest.fixture(autouse=True)
def tier_models(settings):
    """One configured model per tier, with parameter counts so EcoLogits can estimate."""
    # The singleton is cached across tests by django-solo; a stale instance would
    # otherwise carry the previous test's tier_energy into this one.
    cache.clear()
    settings.LLM_CONFIGURATIONS = {
        "small": _llm("small", total_params_b=8),
        "medium": _llm("medium", total_params_b=70),
        "large": _llm("large", total_params_b=120),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "small"
    tier_settings = models.RoutingTierSettings.get_solo()
    tier_settings.simple_model_hrid = "small"
    tier_settings.standard_model_hrid = "medium"
    tier_settings.complex_model_hrid = "large"
    tier_settings.save()
    yield tier_settings
    cache.clear()


def _assistant_message(tier: str, co2: float | None) -> UIMessage:
    metadata = {"tier": tier}
    if co2 is not None:
        metadata["co2_impact"] = co2
    return UIMessage(
        id="a1",
        role="assistant",
        content="Bonjour",
        parts=[TextUIPart(type="text", text="Bonjour")],
        metadata=metadata,
    )


def test_a_small_sample_keeps_the_ecologits_starting_values(tier_models):
    """Under 500 answers a tier is estimated, with the spec's 1 / 8 / 10-ish ratios."""
    ArenaComparisonFactory(
        tier=RoutingTier.SIMPLE.value,
        champion_co2_impact=CO2_PER_ANSWER,
        challenger_co2_impact=CO2_PER_ANSWER,
    )

    payload = tier_energy.collect_tier_energy()

    assert {entry["source"] for entry in payload.values()} == {"estimated"}
    assert payload["simple"]["n"] == 2
    assert payload["standard"]["n"] == 0
    # The estimate grows with the tier: bigger model, longer answer, reasoning tokens.
    assert (
        payload["simple"]["wh_per_answer"]
        < payload["standard"]["wh_per_answer"]
        < payload["complex"]["wh_per_answer"]
    )
    assert energy_ratio(payload, RoutingTier.STANDARD) > 1


def test_a_large_sample_is_measured_from_our_own_co2_figures(tier_models):
    """500 answers on a tier switch it to `measured`, at the mean Wh of those answers."""
    experiment = ArenaComparisonFactory(tier=RoutingTier.STANDARD.value).experiment
    models.ArenaComparison.objects.all().delete()
    models.ArenaComparison.objects.bulk_create(
        [
            models.ArenaComparison(
                experiment=experiment,
                champion_model_hrid="medium",
                challenger_model_hrid="small",
                champion_side="left",
                tier=RoutingTier.STANDARD.value,
                champion_co2_impact=CO2_PER_ANSWER,
                challenger_co2_impact=CO2_PER_ANSWER,
            )
            for _ in range(tier_energy.MIN_ANSWERS_FOR_MEASURED // 2)
        ]
    )

    payload = tier_energy.collect_tier_energy()

    assert payload["standard"]["n"] == tier_energy.MIN_ANSWERS_FOR_MEASURED
    assert payload["standard"]["source"] == "measured"
    assert payload["standard"]["wh_per_answer"] == round(wh_from_co2_kg(CO2_PER_ANSWER), 4)
    assert payload["simple"]["source"] == "estimated"


def test_committed_answers_of_conversations_count_too(tier_models):
    """Assistant message metadata carries the tier and the CO2 of every routed turn."""
    ChatConversationFactory(
        messages=[
            UIMessage(
                id="u0", role="user", content="Hi", parts=[TextUIPart(type="text", text="Hi")]
            ),
            _assistant_message(RoutingTier.COMPLEX.value, CO2_PER_ANSWER),
            _assistant_message(RoutingTier.COMPLEX.value, None),
            _assistant_message("", CO2_PER_ANSWER),
        ]
    )

    payload = tier_energy.collect_tier_energy()

    assert payload["complex"]["n"] == 1


def test_seed_rows_and_old_rows_are_ignored(tier_models):
    """Demo seed data and answers outside the window never move the figure."""
    ArenaComparisonFactory(
        tier=RoutingTier.SIMPLE.value,
        is_seed=True,
        champion_co2_impact=CO2_PER_ANSWER,
        challenger_co2_impact=CO2_PER_ANSWER,
    )
    old = ArenaComparisonFactory(
        tier=RoutingTier.SIMPLE.value,
        champion_co2_impact=CO2_PER_ANSWER,
        challenger_co2_impact=CO2_PER_ANSWER,
    )
    models.ArenaComparison.objects.filter(pk=old.pk).update(
        drawn_at=timezone.now() - timedelta(days=90)
    )

    payload = tier_energy.collect_tier_energy()

    assert payload["simple"]["n"] == 0


def test_command_writes_the_tier_settings(tier_models):
    """The command stores the payload where the tiers endpoint reads it."""
    call_command("refresh_tier_energy", "--days", "7")

    tier_models.refresh_from_db()
    entry = tier_models.tier_energy["simple"]
    assert set(entry) == {"wh_per_answer", "n", "source", "updated_at", "window_days"}
    assert entry["window_days"] == 7
    assert entry["source"] == "estimated"
    assert energy_ratio(tier_models.tier_energy, RoutingTier.COMPLEX) > 1


def test_dry_run_writes_nothing(tier_models):
    call_command("refresh_tier_energy", "--dry-run")

    tier_models.refresh_from_db()
    assert tier_models.tier_energy == {}
