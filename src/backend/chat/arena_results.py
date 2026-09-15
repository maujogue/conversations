"""Aggregations behind the arena results page.

Everything here is read-only and deliberately simple: every vote is a challenger
against the same champion, so a sorted win-rate table is the scoreboard and no
pairwise ranking model is needed. Win rates come with a Wilson score interval and
an "indicative" flag under the experiment's vote threshold. The flag is a label,
never a gate: the numbers are always shown.
"""

import math
from collections import defaultdict
from decimal import Decimal

from chat import models
from chat.arena import get_refused_draws
from chat.enums import ArenaComparisonStatus, ArenaRole, ArenaSide, ArenaVoteOutcome

Z_95 = 1.96
MTOK = Decimal(1_000_000)


def wilson_interval(wins: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """95 percent Wilson score interval of a proportion, as (low, high) in [0, 1]."""
    if total <= 0:
        return (0.0, 0.0)
    p_hat = wins / total
    denominator = 1 + z**2 / total
    centre = (p_hat + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(p_hat * (1 - p_hat) / total + z**2 / (4 * total**2)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def answer_cost_eur(prompt_tokens, completion_tokens, input_price, output_price) -> Decimal:
    """Cost of one answer from its token counts and prices per million tokens."""
    prompt = Decimal(prompt_tokens or 0)
    completion = Decimal(completion_tokens or 0)
    return (prompt * Decimal(input_price or 0) + completion * Decimal(output_price or 0)) / MTOK


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _is_decisive(comparison) -> bool:
    """A vote that picked a side. Ties and "both bad" are votes but not wins."""
    return comparison.winner in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER)


def _draw_counts(votes) -> dict:
    return {
        "ties": sum(1 for c in votes if c.winner == ArenaVoteOutcome.TIE),
        "both_bad": sum(1 for c in votes if c.winner == ArenaVoteOutcome.BOTH_BAD),
    }


def _rate_block(wins: int, total: int, threshold: int) -> dict:
    low, high = wilson_interval(wins, total)
    return {
        "votes": total,
        "wins": wins,
        "win_rate": wins / total if total else None,
        "ci_low": low if total else None,
        "ci_high": high if total else None,
        "indicative": total < threshold,
    }


def build_results(  # pylint: disable=too-many-locals
    experiment: models.ArenaExperiment, include_seed: bool = True
) -> dict:
    """Compute every number the results template renders for one experiment."""
    comparisons = list(experiment.comparisons.all())
    has_seed = any(c.is_seed for c in comparisons)
    if not include_seed:
        comparisons = [c for c in comparisons if not c.is_seed]

    challenger_prices = {
        c.model_hrid: (c.input_price_eur_per_mtok, c.output_price_eur_per_mtok)
        for c in experiment.challengers.all()
    }
    champion_prices = (
        experiment.champion_input_price_eur_per_mtok,
        experiment.champion_output_price_eur_per_mtok,
    )
    threshold = experiment.min_votes_for_conclusion

    by_status = defaultdict(int)
    total_cost = Decimal(0)
    for comparison in comparisons:
        by_status[comparison.status] += 1
        total_cost += answer_cost_eur(
            comparison.champion_prompt_tokens,
            comparison.champion_completion_tokens,
            *champion_prices,
        )
        total_cost += answer_cost_eur(
            comparison.challenger_prompt_tokens,
            comparison.challenger_completion_tokens,
            *challenger_prices.get(comparison.challenger_model_hrid, (0, 0)),
        )

    voted = [c for c in comparisons if c.status == ArenaComparisonStatus.VOTED]
    closed = voted + [c for c in comparisons if c.status == ArenaComparisonStatus.ABANDONED]

    header = {
        "total": len(comparisons),
        "voted": len(voted),
        **_draw_counts(voted),
        "abandoned": by_status[ArenaComparisonStatus.ABANDONED],
        "errored": by_status[ArenaComparisonStatus.ERRORED],
        "pending": by_status[ArenaComparisonStatus.PENDING],
        "vote_rate": len(voted) / len(closed) if closed else None,
        "total_cost_eur": total_cost,
        "refused_draws": get_refused_draws(experiment.pk),
        "has_seed": has_seed,
        "include_seed": include_seed,
        "threshold": threshold,
    }

    # Challenger table, sorted by win rate: the scoreboard.
    hrids = sorted(
        set(challenger_prices) | {c.challenger_model_hrid for c in comparisons},
    )
    challengers = []
    for hrid in hrids:
        rows = [c for c in comparisons if c.challenger_model_hrid == hrid]
        votes = [c for c in rows if c.status == ArenaComparisonStatus.VOTED]
        decisive = [c for c in votes if _is_decisive(c)]
        wins = sum(1 for c in decisive if c.winner == ArenaRole.CHALLENGER)
        prices = challenger_prices.get(hrid, (0, 0))
        challenger_costs = [
            answer_cost_eur(c.challenger_prompt_tokens, c.challenger_completion_tokens, *prices)
            for c in rows
            if c.challenger_prompt_tokens is not None
        ]
        champion_costs = [
            answer_cost_eur(
                c.champion_prompt_tokens, c.champion_completion_tokens, *champion_prices
            )
            for c in rows
            if c.champion_prompt_tokens is not None
        ]
        mean_challenger_cost = _mean(challenger_costs)
        mean_champion_cost = _mean(champion_costs)
        challengers.append(
            {
                "model_hrid": hrid,
                **_rate_block(wins, len(decisive), threshold),
                **_draw_counts(votes),
                "comparisons": len(rows),
                "abandoned": sum(1 for c in rows if c.status == ArenaComparisonStatus.ABANDONED),
                "errored": sum(1 for c in rows if c.status == ArenaComparisonStatus.ERRORED),
                "mean_challenger_cost_eur": mean_challenger_cost,
                "mean_champion_cost_eur": mean_champion_cost,
                "cost_ratio": (
                    float(mean_champion_cost) / float(mean_challenger_cost)
                    if mean_challenger_cost and mean_champion_cost is not None
                    else None
                ),
                "mean_completion_tokens": _mean(c.challenger_completion_tokens for c in rows),
                "mean_latency_ms": _mean(c.challenger_latency_ms for c in rows),
                "mean_first_token_ms": _mean(c.challenger_first_token_ms for c in rows),
                "mean_co2_kg": _mean(c.challenger_co2_impact for c in rows),
                "champion_mean_latency_ms": _mean(c.champion_latency_ms for c in rows),
                "champion_mean_completion_tokens": _mean(
                    c.champion_completion_tokens for c in rows
                ),
                "mean_time_to_vote_s": (
                    (_mean(c.time_to_vote_ms for c in votes) or 0) / 1000 if votes else None
                ),
            }
        )
    challengers.sort(key=lambda row: (row["win_rate"] is None, -(row["win_rate"] or 0)))

    # Champion row: the same stats, computed across every comparison so it can be read
    # side by side with each challenger row above.
    decisive_votes = [c for c in voted if _is_decisive(c)]
    champion_wins = sum(1 for c in decisive_votes if c.winner == ArenaRole.CHAMPION)
    champion_costs = [
        answer_cost_eur(c.champion_prompt_tokens, c.champion_completion_tokens, *champion_prices)
        for c in comparisons
        if c.champion_prompt_tokens is not None
    ]
    champion = {
        "model_hrid": experiment.champion_model_hrid,
        **_rate_block(champion_wins, len(decisive_votes), threshold),
        **_draw_counts(voted),
        "comparisons": len(comparisons),
        "abandoned": header["abandoned"],
        "errored": header["errored"],
        "mean_champion_cost_eur": _mean(champion_costs),
        "mean_completion_tokens": _mean(c.champion_completion_tokens for c in comparisons),
        "mean_latency_ms": _mean(c.champion_latency_ms for c in comparisons),
        "mean_first_token_ms": _mean(c.champion_first_token_ms for c in comparisons),
        "mean_co2_kg": _mean(c.champion_co2_impact for c in comparisons),
        "mean_time_to_vote_s": (_mean(c.time_to_vote_ms for c in voted) or 0) / 1000
        if voted
        else None,
    }

    # Win rate by context tag, per challenger.
    by_context = []
    for hrid in hrids:
        votes = [
            c
            for c in comparisons
            if c.challenger_model_hrid == hrid
            and c.status == ArenaComparisonStatus.VOTED
            and _is_decisive(c)
        ]
        tags = sorted({tag for c in votes for tag in c.context_tags})
        for tag in tags:
            tagged = [c for c in votes if tag in c.context_tags]
            wins = sum(1 for c in tagged if c.winner == ArenaRole.CHALLENGER)
            by_context.append(
                {"model_hrid": hrid, "tag": tag, **_rate_block(wins, len(tagged), threshold)}
            )

    # Position check: the left column should win about half the time.
    left_wins = sum(
        1
        for c in voted
        if (c.winner == ArenaRole.CHAMPION and c.champion_side == ArenaSide.LEFT)
        or (c.winner == ArenaRole.CHALLENGER and c.champion_side == ArenaSide.RIGHT)
    )
    position = _rate_block(left_wins, len(decisive_votes), threshold)

    return {
        "header": header,
        "champion": champion,
        "challengers": challengers,
        "by_context": by_context,
        "position": position,
    }
