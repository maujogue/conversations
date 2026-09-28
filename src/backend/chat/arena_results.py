"""Aggregations behind the arena results page.

Everything here is read-only and deliberately simple: every vote is a challenger
against the same champion, so a sorted win-rate table is the scoreboard and no
pairwise ranking model is needed. Win rates come with a Wilson score interval and
an "indicative" flag while the sample is small or its interval includes 50%.
The flag is a label, never a gate: the numbers are always shown.
"""

import math
from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from chat import models
from chat.arena import get_refused_draws
from chat.enums import (
    ArenaComparisonStatus,
    ArenaOrigin,
    ArenaRole,
    ArenaSide,
    ArenaVoteOutcome,
)
from chat.footprint import wh_from_co2_kg

Z_95 = 1.96
MTOK = Decimal(1_000_000)

# Verdicts of the promotion rule (router spec 8.1).
VERDICT_PROMOTE = "promote"
VERDICT_PROMOTE_FOR_FOOTPRINT = "promote_for_footprint"
VERDICT_KEEP = "keep"
VERDICT_INDICATIVE = "indicative"

# How much lighter in Wh per answer a challenger must be to be promoted on its
# footprint alone, when the vote does not separate it from the champion.
FOOTPRINT_MARGIN = 0.30


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


def recorded_answer_cost(comparison, role) -> Decimal | None:
    """Never reprice historical inference using the experiment's current settings."""
    prices = comparison.price_snapshot.get(role)
    if not prices or prices.get("input") is None or prices.get("output") is None:
        return None
    return answer_cost_eur(
        getattr(comparison, f"{role}_prompt_tokens"),
        getattr(comparison, f"{role}_completion_tokens"),
        prices["input"],
        prices["output"],
    )


def _failure_block(comparisons, role):
    attempts = [c for c in comparisons if getattr(c, f"{role}_started_at") or c.side_finished(role)]
    failures = sum(
        bool(getattr(c, f"{role}_error"))
        and getattr(c, f"{role}_error")
        not in (
            "cancelled",
            "unfinished when the comparison was resolved",
        )
        for c in attempts
    )
    return {
        "attempts": len(attempts),
        "failures": failures,
        "failure_rate": failures / len(attempts) if attempts else None,
    }


def _reason_counts(comparisons):
    counts = defaultdict(int)
    for comparison in comparisons:
        if comparison.status == ArenaComparisonStatus.ERRORED:
            counts[comparison.closed_reason or "legacy_unknown"] += 1
    return counts


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
        "sample_sufficient": total >= threshold,
        "evidence_sufficient": total >= threshold and (low > 0.5 or high < 0.5),
        "indicative": total < threshold or low <= 0.5 <= high,
    }


def _cost_comparison(champion_cost, challenger_cost) -> dict:
    """How the challenger's cost per answer compares, as a multiple at or above 1.

    ``cost_multiple`` is always >= 1 and ``cost_direction`` says which way it goes,
    so the table never has to render "x0.02 cheaper" for an answer that costs fifty
    times more.
    """
    if not challenger_cost or champion_cost is None or not champion_cost:
        return {"cost_multiple": None, "cost_direction": None}
    ratio = float(champion_cost) / float(challenger_cost)
    if ratio >= 1:
        return {"cost_multiple": ratio, "cost_direction": "cheaper"}
    return {"cost_multiple": 1 / ratio, "cost_direction": "dearer"}


def _percentile(values, fraction: float) -> float | None:
    """Nearest-rank percentile of a list of numbers, ``None`` when empty."""
    numbers = sorted(v for v in values if v is not None)
    if not numbers:
        return None
    rank = max(1, math.ceil(fraction * len(numbers)))
    return float(numbers[min(rank, len(numbers)) - 1])


def _mean_wh(comparisons, role) -> float | None:
    """Mean Wh per answer of one role, derived from the stored CO2 figures (spec 10)."""
    return _mean(wh_from_co2_kg(getattr(c, f"{role}_co2_impact")) for c in comparisons)


def _verdict(row: dict, threshold: int) -> str:
    """Promotion verdict of a challenger row (router spec 8.1).

    ``promote``               n >= min_votes_for_conclusion and the Wilson lower
                              bound is above 50 percent.
    ``promote_for_footprint`` the interval still includes 50 percent, but the
                              challenger is at least ``FOOTPRINT_MARGIN`` lighter in
                              Wh per answer and no slower at p95.
    ``keep``                  enough votes, no reason to move.
    ``indicative``            under the vote threshold, nothing to conclude.
    """
    if not row["sample_sufficient"] or row["ci_low"] is None:
        return VERDICT_INDICATIVE
    if row["ci_low"] > 0.5:
        return VERDICT_PROMOTE
    lighter = row["wh_ratio"] is not None and row["wh_ratio"] <= 1 - FOOTPRINT_MARGIN
    not_slower = (
        row["p95_latency_ms"] is not None
        and row["champion_p95_latency_ms"] is not None
        and row["p95_latency_ms"] <= row["champion_p95_latency_ms"]
    )
    if row["ci_low"] <= 0.5 <= row["ci_high"] and lighter and not_slower:
        return VERDICT_PROMOTE_FOR_FOOTPRINT
    return VERDICT_KEEP


def _decisive_votes(comparisons) -> list:
    return [c for c in comparisons if c.status == ArenaComparisonStatus.VOTED and _is_decisive(c)]


def _challenger_wins(comparisons) -> int:
    return sum(1 for c in comparisons if c.winner == ArenaRole.CHALLENGER)


def _challenger_row(hrid: str, rows: list, manual_rows: list, threshold: int) -> dict:
    """One line of the scoreboard: win rate, cost, footprint and latency of a challenger.

    ``rows`` are its sampled comparisons, the only ones the win rate and the verdict
    are built from; ``manual_rows`` are the second opinions on the same model, kept in
    their own columns (router spec 8.1).
    """
    votes = [c for c in rows if c.status == ArenaComparisonStatus.VOTED]
    decisive = _decisive_votes(rows)
    challenger_costs = [
        recorded_answer_cost(c, ArenaRole.CHALLENGER)
        for c in rows
        if c.challenger_prompt_tokens is not None
    ]
    champion_costs = [
        recorded_answer_cost(c, ArenaRole.CHAMPION)
        for c in rows
        if c.champion_prompt_tokens is not None
    ]
    mean_challenger_cost = _mean(challenger_costs)
    mean_champion_cost = _mean(champion_costs)
    mean_wh = _mean_wh(rows, ArenaRole.CHALLENGER)
    champion_mean_wh = _mean_wh(rows, ArenaRole.CHAMPION)
    manual_decisive = _decisive_votes(manual_rows)
    row = {
        "model_hrid": hrid,
        **_rate_block(_challenger_wins(decisive), len(decisive), threshold),
        **_draw_counts(votes),
        "comparisons": len(rows),
        **_failure_block(rows, ArenaRole.CHALLENGER),
        "abandoned": sum(1 for c in rows if c.status == ArenaComparisonStatus.ABANDONED),
        "errored": sum(1 for c in rows if c.status == ArenaComparisonStatus.ERRORED),
        "mean_challenger_cost_eur": mean_challenger_cost,
        "mean_champion_cost_eur": mean_champion_cost,
        "cost_ratio": (
            float(mean_champion_cost) / float(mean_challenger_cost)
            if mean_challenger_cost and mean_champion_cost is not None
            else None
        ),
        # The ratio alone reads wrong in the table: a challenger at 0.02 is not
        # "x0.02 cheaper", it is fifty times dearer. Render the multiple and the
        # direction instead, both derived from the same number.
        **_cost_comparison(mean_champion_cost, mean_challenger_cost),
        "mean_completion_tokens": _mean(c.challenger_completion_tokens for c in rows),
        "mean_reasoning_tokens": _mean(c.challenger_reasoning_tokens for c in rows),
        "mean_latency_ms": _mean(c.challenger_latency_ms for c in rows),
        "mean_first_token_ms": _mean(c.challenger_first_token_ms for c in rows),
        "mean_co2_kg": _mean(c.challenger_co2_impact for c in rows),
        "mean_wh_per_answer": mean_wh,
        "champion_mean_wh_per_answer": champion_mean_wh,
        "wh_ratio": (mean_wh / champion_mean_wh if mean_wh and champion_mean_wh else None),
        "p95_latency_ms": _percentile([c.challenger_latency_ms for c in rows], 0.95),
        "champion_p95_latency_ms": _percentile([c.champion_latency_ms for c in rows], 0.95),
        "champion_mean_latency_ms": _mean(c.champion_latency_ms for c in rows),
        "champion_mean_completion_tokens": _mean(c.champion_completion_tokens for c in rows),
        "mean_time_to_vote_s": (
            (_mean(c.time_to_vote_ms for c in votes) or 0) / 1000 if votes else None
        ),
        # Second opinions asked for by users. Never mixed into the sampled win rate:
        # the user chose to run this model, which is not the same population.
        "manual": {
            "comparisons": len(manual_rows),
            **_rate_block(_challenger_wins(manual_decisive), len(manual_decisive), threshold),
        },
    }
    row["verdict"] = _verdict(row, threshold)
    return row


def _tag_rows(votes: list, hrids: list, field: str, threshold: int) -> list[dict]:
    """Win rate per value of a tag field, per challenger (router spec 8.1).

    ``field`` is either a single-valued label (``domain``, ``task``) or the
    ``context_tags`` list; a comparison counts once per tag it carries.
    """
    rows = []
    for hrid in hrids:
        tagged = [c for c in votes if c.challenger_model_hrid == hrid]
        values = sorted({tag for c in tagged for tag in _tag_values(c, field)})
        for value in values:
            matching = [c for c in tagged if value in _tag_values(c, field)]
            rows.append(
                {
                    "model_hrid": hrid,
                    "tag": value,
                    **_rate_block(_challenger_wins(matching), len(matching), threshold),
                }
            )
    return rows


def _tag_values(comparison, field: str) -> list[str]:
    value = getattr(comparison, field)
    if isinstance(value, list):
        return [tag for tag in value if tag]
    return [value] if value else []


def build_results(  # pylint: disable=too-many-locals
    experiment: models.ArenaExperiment, include_seed: bool = True
) -> dict:
    """Compute every number the results template renders for one experiment."""
    comparisons = list(
        experiment.comparisons.defer(
            "champion_payload",
            "challenger_payload",
            "input_snapshot",
        )
    )
    has_seed = any(c.is_seed for c in comparisons)
    if not include_seed:
        comparisons = [c for c in comparisons if not c.is_seed]
    challengers_by_hrid = {c.model_hrid: c for c in experiment.challengers.all()}
    control_hrids = {hrid for hrid, row in challengers_by_hrid.items() if row.is_control}
    threshold = experiment.min_votes_for_conclusion

    # Three populations, never mixed: the sampled draws the scoreboard is made of,
    # the second opinions users asked for, and the out-of-tier control challenger.
    manual = [c for c in comparisons if c.origin == ArenaOrigin.MANUAL]
    drawn = [c for c in comparisons if c.origin != ArenaOrigin.MANUAL]
    control = [c for c in drawn if c.challenger_model_hrid in control_hrids]
    sampled = [c for c in drawn if c.challenger_model_hrid not in control_hrids]

    by_status = defaultdict(int)
    costs = []
    for comparison in comparisons:
        by_status[comparison.status] += 1
        for role in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER):
            if getattr(comparison, f"{role}_prompt_tokens") is not None:
                costs.append(recorded_answer_cost(comparison, role))

    voted = [c for c in comparisons if c.status == ArenaComparisonStatus.VOTED]
    closed = [c for c in comparisons if c.status != ArenaComparisonStatus.PENDING]
    known_costs = [cost for cost in costs if cost is not None]
    manual_decisive = _decisive_votes(manual)
    header = {
        "total": len(comparisons),
        "voted": len(voted),
        **_draw_counts(voted),
        "abandoned": by_status[ArenaComparisonStatus.ABANDONED],
        "errored": by_status[ArenaComparisonStatus.ERRORED],
        "pending": by_status[ArenaComparisonStatus.PENDING],
        "vote_rate": len(voted) / len(comparisons) if comparisons else None,
        "closed_vote_rate": len(voted) / len(closed) if closed else None,
        "closed": len(closed),
        "total_cost_eur": sum(known_costs, Decimal(0)) if known_costs else None,
        "unpriced_answers": len(costs) - len(known_costs),
        "failure_reasons": dict(_reason_counts(comparisons)),
        "refused_draws": get_refused_draws(experiment.pk),
        "has_seed": has_seed,
        "include_seed": include_seed,
        "threshold": threshold,
        "tier": experiment.tier,
        "sampled": len(sampled),
        "manual": {
            "comparisons": len(manual),
            **_rate_block(_challenger_wins(manual_decisive), len(manual_decisive), threshold),
        },
        "control_comparisons": len(control),
    }

    # Challenger table, sorted by win rate: the scoreboard.
    hrids = sorted(
        (set(challengers_by_hrid) | {c.challenger_model_hrid for c in sampled}) - control_hrids
    )
    challengers = [
        _challenger_row(
            hrid,
            [c for c in sampled if c.challenger_model_hrid == hrid],
            [c for c in manual if c.challenger_model_hrid == hrid],
            threshold,
        )
        for hrid in hrids
    ]
    challengers.sort(key=lambda row: (row["win_rate"] is None, -(row["win_rate"] or 0)))

    # The control challenger of section 5.4 has its own table: it measures the
    # classifier threshold, never the tier's champion.
    controls = [
        _challenger_row(
            hrid, [c for c in control if c.challenger_model_hrid == hrid], [], threshold
        )
        for hrid in sorted(control_hrids)
    ]

    # Champion row: the same stats, computed across every sampled comparison so it can
    # be read side by side with each challenger row above.
    decisive_votes = _decisive_votes(sampled)
    champion_wins = sum(1 for c in decisive_votes if c.winner == ArenaRole.CHAMPION)
    champion_costs = [
        recorded_answer_cost(c, ArenaRole.CHAMPION)
        for c in sampled
        if c.champion_prompt_tokens is not None
    ]
    sampled_voted = [c for c in sampled if c.status == ArenaComparisonStatus.VOTED]
    champion = {
        "model_hrid": experiment.champion_model_hrid,
        **_failure_block(sampled, ArenaRole.CHAMPION),
        **_rate_block(champion_wins, len(decisive_votes), threshold),
        **_draw_counts(sampled_voted),
        "comparisons": len(sampled),
        "abandoned": sum(1 for c in sampled if c.status == ArenaComparisonStatus.ABANDONED),
        "errored": sum(1 for c in sampled if c.status == ArenaComparisonStatus.ERRORED),
        "mean_champion_cost_eur": _mean(champion_costs),
        "mean_completion_tokens": _mean(c.champion_completion_tokens for c in sampled),
        "mean_reasoning_tokens": _mean(c.champion_reasoning_tokens for c in sampled),
        "mean_latency_ms": _mean(c.champion_latency_ms for c in sampled),
        "mean_first_token_ms": _mean(c.champion_first_token_ms for c in sampled),
        "mean_co2_kg": _mean(c.champion_co2_impact for c in sampled),
        "mean_wh_per_answer": _mean_wh(sampled, ArenaRole.CHAMPION),
        "mean_time_to_vote_s": (
            (_mean(c.time_to_vote_ms for c in sampled_voted) or 0) / 1000 if sampled_voted else None
        ),
    }

    # Win rate per tag, per challenger: the preference dataset the tags exist for.
    sampled_decisive = _decisive_votes(sampled)
    tag_tables = {
        f"by_{field}": _tag_rows(sampled_decisive, hrids, field, threshold)
        for field in ("domain", "task", "context_tags")
    }

    # Position check: the left column should win about half the time.
    left_wins = sum(
        1
        for c in sampled_voted
        if (c.winner == ArenaRole.CHAMPION and c.champion_side == ArenaSide.LEFT)
        or (c.winner == ArenaRole.CHALLENGER and c.champion_side == ArenaSide.RIGHT)
    )
    position = _rate_block(left_wins, len(decisive_votes), threshold)

    return {
        "header": header,
        "champion": champion,
        "challengers": challengers,
        "controls": controls,
        **tag_tables,
        "by_context": tag_tables["by_context_tags"],
        "position": position,
    }


def challenger_row(results: dict, model_hrid: str) -> dict | None:
    """The scoreboard row of one challenger, or ``None`` when it has no sampled row."""
    return next(
        (row for row in results["challengers"] if row["model_hrid"] == model_hrid),
        None,
    )


def promotion_evidence(experiment, row: dict) -> dict:
    """What the history row keeps about why a challenger was promoted."""
    return {
        "experiment_id": str(experiment.pk),
        "experiment_name": experiment.name,
        "votes": row["votes"],
        "wins": row["wins"],
        "win_rate": row["win_rate"],
        "ci_low": row["ci_low"],
        "ci_high": row["ci_high"],
        "verdict": row["verdict"],
        "wh_ratio": row["wh_ratio"],
        "wh_per_answer": row["mean_wh_per_answer"],
        "champion_wh_per_answer": row["champion_mean_wh_per_answer"],
        "p95_latency_ms": row["p95_latency_ms"],
        "champion_p95_latency_ms": row["champion_p95_latency_ms"],
    }


@transaction.atomic
def promote_challenger(experiment, model_hrid: str, user=None) -> models.RoutingTierHistory:
    """Make a challenger the model of the experiment's tier (router spec 8.1, PR 15).

    The former champion is kept as an alternative of the tier, so the arena can go on
    comparing against it, and the reason is appended to ``RoutingTierHistory`` with the
    evidence the decision was taken on. Promotion is manual and always human: this
    function only records what the admin decided.
    """
    row = challenger_row(build_results(experiment), model_hrid)
    if row is None:
        raise ValueError(f"{model_hrid} is not a challenger of this experiment.")

    tier = experiment.tier
    tier_settings = models.RoutingTierSettings.get_solo()
    previous = tier_settings.model_for(tier)
    if previous == model_hrid:
        raise ValueError(f"{model_hrid} is already the {tier} tier model.")
    alternatives = [hrid for hrid in tier_settings.alternatives_for(tier) if hrid != model_hrid]
    alternatives.append(previous)
    setattr(tier_settings, f"{tier}_model_hrid", model_hrid)
    setattr(tier_settings, f"{tier}_alternatives", alternatives)
    tier_settings.save()

    return models.RoutingTierHistory.objects.create(
        tier=tier,
        changed_by=user if user is not None and user.is_authenticated else None,
        reason=models.RoutingTierHistory.Reason.PROMOTION,
        from_value=previous,
        to_value=model_hrid,
        evidence=promotion_evidence(experiment, row),
    )
