"""Tests for the demo seed: the three tier experiments and the shared constants."""

# pylint: disable=redefined-outer-name, unused-argument

from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from chat import demo_seed
from chat.enums import ArenaComparisonStatus, RoutingTier
from chat.factories import ArenaExperimentFactory
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import ArenaComparison, ArenaExperiment, RoutingTierSettings

pytestmark = pytest.mark.django_db


def _llm(
    hrid: str, input_price: float, output_price: float, total_b: float, active_b: float = None
) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        system_prompt="You are a helpful assistant.",
        tools=[],
        total_params_b=total_b,
        # Mixture-of-experts models activate a fraction of their weights; EcoLogits
        # reads the active count, so a fixture that sets it to the total would make a
        # sparse 117B model look as heavy as a dense 128B one.
        active_params_b=total_b if active_b is None else active_b,
        input_price_eur_per_mtok=input_price,
        output_price_eur_per_mtok=output_price,
        provider=LLMProvider(hrid="albert", base_url="https://example.invalid/", api_key="k"),
    )


@pytest.fixture(autouse=True)
def models_settings(settings):
    """Every model the demo seed names, priced like the real configuration."""
    settings.LLM_CONFIGURATIONS = {
        "ministral-3-8b": _llm("ministral-3-8b", 0.095, 0.095, 8),
        "mistral-small-3-2": _llm("mistral-small-3-2", 0.065, 0.173, 24),
        "gpt-oss-120b": _llm("gpt-oss-120b", 0.026, 0.147, 117, active_b=5.1),
        "mistral-medium-3-5": _llm("mistral-medium-3-5", 1.3, 6.5, 128),
        "gemma-4-31b": _llm("gemma-4-31b", 0.078, 0.295, 31),
        "deepseek-v4-flash": _llm("deepseek-v4-flash", 0.043, 0.121, 284, active_b=13),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "ministral-3-8b"


@pytest.fixture
def tiers():
    """The spec's tier assignment, as seed_routing_tiers would leave it."""
    cache.clear()
    call_command("seed_routing_tiers", "--force", stdout=StringIO())
    yield RoutingTierSettings.get_solo()
    cache.clear()


def test_one_active_experiment_per_tier_at_the_demo_settings(tiers):
    """Three experiments, all active at once, at 10 percent sampling and a 100/day cap."""
    call_command("seed_router_demo", "--count", "40", stdout=StringIO())

    experiments = {e.tier: e for e in ArenaExperiment.objects.filter(name__startswith="Demo ")}
    assert set(experiments) == {tier.value for tier in RoutingTier}
    for experiment in experiments.values():
        assert experiment.is_active is True
        assert experiment.sampling_rate == demo_seed.SAMPLING_RATE == 0.10
        assert experiment.daily_cap_per_user == demo_seed.DAILY_CAP_PER_USER == 100


def test_every_seeded_comparison_is_flagged(tiers):
    """Nothing the demo writes may pass for a real vote."""
    call_command("seed_router_demo", "--count", "40", stdout=StringIO())

    assert ArenaComparison.objects.exists()
    assert not ArenaComparison.objects.filter(is_seed=False).exists()


def test_a_running_experiment_is_never_deactivated_silently(tiers):
    """A tier held by someone else's experiment stops the command, and says what it holds."""
    other = ArenaExperimentFactory(tier=RoutingTier.SIMPLE.value, is_active=True)

    with pytest.raises(CommandError, match=other.name):
        call_command("seed_router_demo", "--count", "10", stdout=StringIO())

    other.refresh_from_db()
    assert other.is_active is True

    call_command("seed_router_demo", "--count", "10", "--deactivate-others", stdout=StringIO())
    other.refresh_from_db()
    assert other.is_active is False


def test_win_rates_follow_the_benchmark_anchors(tiers):
    """The seeded preference reproduces the standing recorded in demo_seed.

    The real vote rate is 5 percent, so a demo-sized run gives too few votes to
    assert on. This drives the underlying command at a 100 percent vote rate: the
    win rate is the thing under test, not how many people bother to vote.
    """
    call_command("seed_router_demo", "--count", "10", stdout=StringIO())
    experiment = ArenaExperiment.objects.get(name="Demo complex")
    ArenaComparison.objects.filter(experiment=experiment).delete()

    target = demo_seed.CHALLENGER_WIN_RATES[RoutingTier.COMPLEX]["mistral-medium-3-5"]
    call_command(
        "seed_arena_comparisons",
        "--experiment",
        str(experiment.pk),
        "--count",
        "1500",
        "--vote-rate",
        "1.0",
        "--error-rate",
        "0.0",
        "--win-rate",
        f"mistral-medium-3-5={target}",
        "--win-rate",
        "deepseek-v4-flash=0.48",
        "--seed",
        "7",
        stdout=StringIO(),
    )

    voted = ArenaComparison.objects.filter(
        experiment=experiment,
        status=ArenaComparisonStatus.VOTED,
        challenger_model_hrid="mistral-medium-3-5",
        winner__in=["champion", "challenger"],
    )
    observed = voted.filter(winner="challenger").count() / voted.count()
    assert abs(observed - target) < 0.06
    # The premium model loses its own tier, which is the published standing:
    # Artificial Analysis puts gpt-oss-120b above Mistral Medium 3.
    assert observed < 0.5


def test_the_seeded_vote_count_matches_what_the_tool_collects(tiers):
    """10 percent sampling at a 5 percent vote rate is the 5,000 choices a month."""
    drawn = sum(demo_seed.comparisons_per_tier().values())
    assert drawn == pytest.approx(demo_seed.MONTHLY_TURNS * demo_seed.SAMPLING_RATE, rel=0.01)
    votes = drawn * demo_seed.ARENA_VOTE_RATE
    assert votes == pytest.approx(5000, rel=0.01)


def test_seeded_energy_follows_the_model_not_the_dice(tiers):
    """A dense 128B model must look heavier than a 5.1B-active one on the results page."""
    call_command("seed_router_demo", "--count", "300", "--seed", "3", stdout=StringIO())

    rows = ArenaComparison.objects.filter(
        experiment__name="Demo complex", challenger_model_hrid="mistral-medium-3-5"
    ).exclude(challenger_co2_impact=None)
    assert rows.exists()
    heavy = sum(r.challenger_co2_impact / r.challenger_completion_tokens for r in rows) / len(rows)
    light = sum(r.champion_co2_impact / r.champion_completion_tokens for r in rows) / len(rows)
    assert heavy > light


def test_the_price_snapshot_is_filled_for_both_sides(tiers):
    """An empty champion snapshot silently empties the cost ratio on the results page."""
    call_command("seed_router_demo", "--count", "20", stdout=StringIO())

    for comparison in ArenaComparison.objects.all()[:20]:
        assert comparison.price_snapshot["champion"]["input"]
        assert comparison.price_snapshot["challenger"]["input"]


def test_explain_prints_a_source_for_every_seeded_rate(tiers):
    """Every number the demo shows must be traceable to a published standing."""
    out = StringIO()
    call_command("seed_router_demo", "--explain", stdout=out)
    text = out.getvalue()

    for tier, challengers in demo_seed.CHALLENGER_WIN_RATES.items():
        assert tier.value in text
        for hrid in challengers:
            assert hrid in text
            assert demo_seed.BENCHMARK_ANCHORS[hrid]["anchor"] in text
    assert not ArenaComparison.objects.exists()  # --explain writes nothing


def test_answer_cost_uses_the_configured_prices(settings):
    """The pitch's euro figures are arithmetic on the configuration, not an estimate."""
    configuration = settings.LLM_CONFIGURATIONS["mistral-medium-3-5"]
    cost = demo_seed.answer_cost_eur(configuration, 1000, 500)
    assert cost == pytest.approx((1000 * 1.3 + 500 * 6.5) / 1_000_000)


def test_the_baseline_is_the_dearest_configured_model(settings):
    """The counterfactual must be the model an unrouted deployment would pick."""
    baseline = settings.LLM_CONFIGURATIONS[demo_seed.BASELINE_MODEL_HRID]
    for hrid in demo_seed.TIER_MODEL.values():
        tier_model = settings.LLM_CONFIGURATIONS[hrid]
        assert baseline.output_price_eur_per_mtok > tier_model.output_price_eur_per_mtok
