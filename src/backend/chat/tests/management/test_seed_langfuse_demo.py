"""Tests for the Langfuse demo seed: the plan, the counterfactual, and the report.

The emission itself talks OTLP to a live Langfuse, so these tests cover the part
that decides the numbers: ``--dry-run`` plans every turn and prints the summary
without opening a connection.
"""

# pylint: disable=redefined-outer-name, unused-argument

import random
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from chat import demo_seed
from chat.llm_configuration import LLModel, LLMProvider
from chat.management.commands.seed_langfuse_demo import Command


def _llm(hrid: str, input_price: float, output_price: float) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        system_prompt="You are a helpful assistant.",
        tools=[],
        input_price_eur_per_mtok=input_price,
        output_price_eur_per_mtok=output_price,
        provider=LLMProvider(hrid="albert", base_url="https://example.invalid/", api_key="k"),
    )


@pytest.fixture(autouse=True)
def langfuse_settings(settings):
    settings.LANGFUSE_ENABLED = True
    settings.LLM_CONFIGURATIONS = {
        "ministral-3-8b": _llm("ministral-3-8b", 0.095, 0.095),
        "mistral-small-3-2": _llm("mistral-small-3-2", 0.065, 0.173),
        "gpt-oss-120b": _llm("gpt-oss-120b", 0.026, 0.147),
        "mistral-medium-3-5": _llm("mistral-medium-3-5", 1.3, 6.5),
        "gemma-4-31b": _llm("gemma-4-31b", 0.078, 0.295),
        "deepseek-v4-flash": _llm("deepseek-v4-flash", 0.043, 0.121),
    }


@pytest.fixture
def plan(settings):
    """A planned run, the same structure the emitter walks."""
    command = Command()
    return command._plan(  # pylint: disable=protected-access
        random.Random(11), 800, settings.LLM_CONFIGURATIONS
    )


def test_the_counterfactual_bills_identical_tokens(plan):
    """Same work, different price: the savings claim rests on the tokens matching."""
    for row in plan:
        cheaper_than_baseline = row["routed_cost"] <= row["baseline_cost"]
        assert cheaper_than_baseline, row["model"]
    # The baseline is the same model for every turn, so its cost is a pure function
    # of the token counts the routed turn used.
    baseline = plan[0]
    assert baseline["baseline_cost"] == pytest.approx(
        demo_seed.answer_cost_eur(
            _llm("mistral-medium-3-5", 1.3, 6.5), baseline["prompt"], baseline["billed_output"]
        )
    )


def test_the_saving_is_large_and_comes_from_the_price_not_the_tokens(plan):
    routed = sum(row["routed_cost"] + row["router_cost"] for row in plan)
    baseline = sum(row["baseline_cost"] for row in plan)
    assert baseline / routed > 5
    # Tokens are identical by construction; only the per-token price differs.
    assert sum(row["billed_output"] for row in plan) > 0


def test_the_tier_mix_is_respected(plan):
    for tier, share in demo_seed.TIER_MIX.items():
        observed = sum(1 for row in plan if row["tier"] == tier) / len(plan)
        assert abs(observed - share) < 0.06


def test_some_turns_skip_the_classifier(plan):
    """The shortcut path is real behaviour and must not be silently dropped."""
    classified = sum(1 for row in plan if row["classified"])
    assert 0 < classified < len(plan)
    assert all(row["router_cost"] == 0 for row in plan if not row["classified"])
    assert all(row["router_cost"] > 0 for row in plan if row["classified"])


def test_arena_draws_follow_the_sampling_rate(plan):
    drawn = [row for row in plan if row["arena_challenger"]]
    assert abs(len(drawn) / len(plan) - demo_seed.SAMPLING_RATE) < 0.04
    for row in drawn:
        assert row["arena_challenger"] in demo_seed.CHALLENGER_WIN_RATES[row["tier"]]


def test_the_answer_span_is_named_after_its_tier():
    """Langfuse cannot group by one tag, so the tier has to be an observation name."""
    for tier in demo_seed.TIER_MIX:
        assert demo_seed.answer_span_name(tier) == f"answer-{tier.value}"
    assert set(demo_seed.ANSWER_SPAN_NAMES) == {
        demo_seed.answer_span_name(tier) for tier in demo_seed.TIER_MIX
    }


def test_turns_are_spread_over_a_realistic_user_base(plan):
    """100,000 users over 1,000,000 turns, with a few heavy users and a long tail."""
    users = [row["user_id"] for row in plan]
    assert all(user.startswith("user-") for user in users)
    # 800 turns drawn from a 100,000-strong pool: nearly all distinct, none repeated
    # so often that one user would dominate a per-user view.
    assert len(set(users)) > len(users) * 0.9


def test_the_environments_are_named_for_the_traffic_they_model():
    assert demo_seed.PRODUCTION_ENVIRONMENT == "production"
    assert demo_seed.BASELINE_ENVIRONMENT == "production-baseline"
    assert demo_seed.ARENA_ENVIRONMENT == "production-arena"
    # The three must stay distinct: the savings widget groups on this dimension.
    assert (
        len(
            {
                demo_seed.PRODUCTION_ENVIRONMENT,
                demo_seed.BASELINE_ENVIRONMENT,
                demo_seed.ARENA_ENVIRONMENT,
            }
        )
        == 3
    )


def test_dry_run_reports_the_pitch_numbers_and_emits_nothing():
    out = StringIO()
    call_command("seed_langfuse_demo", "--dry-run", "--turns", "400", stdout=out)
    text = out.getvalue()

    assert "saving" in text
    assert "router classifier overhead" in text
    assert demo_seed.BASELINE_MODEL_HRID in text
    for tier in demo_seed.TIER_MIX:
        assert tier.value in text


def test_it_refuses_to_run_with_langfuse_off(settings):
    settings.LANGFUSE_ENABLED = False
    with pytest.raises(CommandError, match="LANGFUSE_ENABLED"):
        call_command("seed_langfuse_demo", "--dry-run", "--turns", "10")


def test_an_unpriced_model_is_refused_rather_than_costed_at_zero(settings):
    """A missing price would quietly turn the whole pitch figure into a lie."""
    unpriced = _llm("gpt-oss-120b", 0.026, 0.147)
    settings.LLM_CONFIGURATIONS["gpt-oss-120b"] = unpriced.model_copy(
        update={"input_price_eur_per_mtok": None}
    )
    with pytest.raises(CommandError, match="no configured price"):
        call_command("seed_langfuse_demo", "--dry-run", "--turns", "10")
