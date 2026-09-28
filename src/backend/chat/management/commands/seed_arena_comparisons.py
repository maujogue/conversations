"""Seed synthetic arena comparisons so the results page has something to show.

Demo tooling only. Every row is flagged ``is_seed`` and the results page shows a
banner and an "exclude seed data" toggle whenever such rows exist.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from chat.arena import candidate_prices
from chat.enums import ArenaComparisonStatus, ArenaContextTag, ArenaRole, ArenaSide
from chat.footprint import estimate_co2_kg
from chat.models import ArenaComparison, ArenaExperiment

TAG_WEIGHTS = {
    ArenaContextTag.PLAIN: 0.6,
    ArenaContextTag.WEB_SEARCH: 0.15,
    ArenaContextTag.ATTACHMENT: 0.15,
    ArenaContextTag.PROJECT: 0.10,
}


def _co2_kg(hrid: str, completion_tokens: int, latency_ms: int, rng: random.Random):
    """CO2 of one seeded answer, from the model's real parameter counts.

    EcoLogits is given the same inputs the application gives it at write time, so a
    dense 128B model is heavier than a 5.1B-active one - which a random draw would
    not guarantee, and the footprint columns are part of what the demo claims.
    ``None`` when the model has no configured parameter counts.
    """
    configuration = settings.LLM_CONFIGURATIONS.get(hrid)
    if configuration is None:
        return None
    co2 = estimate_co2_kg(configuration, completion_tokens, latency_ms / 1000)
    # A little spread, so the means are not suspiciously identical across rows.
    return co2 * rng.uniform(0.9, 1.1) if co2 else co2


def _random_tags(rng: random.Random) -> list[str]:
    tag = rng.choices(list(TAG_WEIGHTS), weights=list(TAG_WEIGHTS.values()))[0]
    return [tag.value]


class Command(BaseCommand):
    """Create synthetic, clearly flagged arena comparisons for an experiment."""

    help = "Seed synthetic arena comparisons (flagged is_seed) for a demo of the results page."

    def add_arguments(self, parser):
        parser.add_argument("--experiment", required=True, help="Experiment id or exact name.")
        parser.add_argument("--count", type=int, default=300, help="Comparisons to create.")
        parser.add_argument(
            "--challenger-win-rate",
            type=float,
            default=0.55,
            help="Probability that a voted comparison goes to the challenger.",
        )
        parser.add_argument(
            "--vote-rate",
            type=float,
            default=0.35,
            help="Share of comparisons that receive a vote (the rest are abandoned).",
        )
        parser.add_argument(
            "--win-rate",
            action="append",
            default=[],
            metavar="HRID=RATE",
            help=(
                "Per-challenger win rate, repeatable (e.g. --win-rate gemma-4-31b=0.44). "
                "Challengers not listed keep --challenger-win-rate."
            ),
        )
        parser.add_argument("--error-rate", type=float, default=0.02)
        parser.add_argument("--days", type=int, default=30, help="Spread draws over N days.")
        parser.add_argument("--seed", type=int, default=None, help="Random seed.")

    def handle(self, *args, **options):  # pylint: disable=too-many-locals
        experiment = (
            ArenaExperiment.objects.filter(pk=options["experiment"]).first()
            if _looks_like_uuid(options["experiment"])
            else ArenaExperiment.objects.filter(name=options["experiment"]).first()
        )
        if experiment is None:
            raise CommandError(f"No experiment matches {options['experiment']!r}.")
        challengers = list(experiment.challengers.values_list("model_hrid", flat=True))
        if not challengers:
            raise CommandError("The experiment has no challenger.")

        win_rates = _parse_win_rates(options["win_rate"])
        unknown = sorted(set(win_rates) - set(challengers))
        if unknown:
            raise CommandError(f"--win-rate names models that are not challengers: {unknown}.")

        rng = random.Random(options["seed"])  # noqa: S311  # demo data, not security
        now = timezone.now()
        # The reference needs the champion's hrid set: candidate_prices reads it off the
        # comparison, and an unset one silently yields an empty snapshot, which shows up
        # on the results page as an unpriced answer and an empty cost ratio.
        price_reference = ArenaComparison(
            experiment=experiment, champion_model_hrid=experiment.champion_model_hrid
        )
        champion_prices = candidate_prices(price_reference, ArenaRole.CHAMPION)
        challenger_prices = {}
        for hrid in challengers:
            price_reference.challenger_model_hrid = hrid
            challenger_prices[hrid] = candidate_prices(price_reference, ArenaRole.CHALLENGER)
        rows = []
        for _ in range(options["count"]):
            drawn_at = now - timedelta(seconds=rng.uniform(0, options["days"] * 86400))
            champion_side = rng.choice([ArenaSide.LEFT, ArenaSide.RIGHT])
            challenger = rng.choice(challengers)
            roll = rng.random()
            if roll < options["error_rate"]:
                status = ArenaComparisonStatus.ERRORED
            elif roll < options["error_rate"] + options["vote_rate"]:
                status = ArenaComparisonStatus.VOTED
            else:
                status = ArenaComparisonStatus.ABANDONED

            champion_completion = rng.randint(120, 900)
            challenger_completion = rng.randint(120, 900)
            champion_latency_ms = int(champion_completion * rng.uniform(18, 35))
            challenger_latency_ms = int(challenger_completion * rng.uniform(12, 30))
            comparison = ArenaComparison(
                experiment=experiment,
                is_seed=True,
                turn=rng.randint(1, 6),
                context_tags=_random_tags(rng),
                champion_model_hrid=experiment.champion_model_hrid,
                challenger_model_hrid=challenger,
                champion_side=champion_side,
                status=status,
                drawn_at=drawn_at,
                price_snapshot={
                    ArenaRole.CHAMPION: champion_prices,
                    ArenaRole.CHALLENGER: challenger_prices[challenger],
                },
                champion_prompt_tokens=rng.randint(400, 3000),
                champion_completion_tokens=champion_completion,
                champion_latency_ms=champion_latency_ms,
                champion_first_token_ms=rng.randint(300, 1500),
                champion_co2_impact=_co2_kg(
                    experiment.champion_model_hrid, champion_completion, champion_latency_ms, rng
                ),
                champion_finished_at=drawn_at + timedelta(seconds=rng.uniform(4, 25)),
                challenger_prompt_tokens=rng.randint(400, 3000),
                challenger_completion_tokens=challenger_completion,
                challenger_latency_ms=challenger_latency_ms,
                challenger_first_token_ms=rng.randint(200, 1200),
                challenger_co2_impact=_co2_kg(
                    challenger, challenger_completion, challenger_latency_ms, rng
                ),
                challenger_finished_at=drawn_at + timedelta(seconds=rng.uniform(4, 25)),
            )
            if status == ArenaComparisonStatus.VOTED:
                comparison.winner = (
                    ArenaRole.CHALLENGER
                    if rng.random() < win_rates.get(challenger, options["challenger_win_rate"])
                    else ArenaRole.CHAMPION
                )
                comparison.time_to_vote_ms = rng.randint(3000, 60000)
                comparison.voted_at = comparison.challenger_finished_at + timedelta(
                    milliseconds=comparison.time_to_vote_ms
                )
            elif status == ArenaComparisonStatus.ERRORED:
                comparison.closed_reason = "candidate_failed"
                comparison.challenger_error = "model_connection_error"
                comparison.challenger_payload = None
            rows.append(comparison)

        ArenaComparison.objects.bulk_create(rows)
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(rows)} comparisons on '{experiment.name}' "
                f"({sum(1 for r in rows if r.status == ArenaComparisonStatus.VOTED)} voted)."
            )
        )


def _parse_win_rates(pairs: list[str]) -> dict[str, float]:
    """Turn the repeated ``HRID=RATE`` options into a mapping, validating each rate."""
    rates = {}
    for pair in pairs:
        model, _, raw = pair.partition("=")
        if not model or not raw:
            raise CommandError(f"--win-rate expects HRID=RATE, got {pair!r}.")
        try:
            rate = float(raw)
        except ValueError as exc:
            raise CommandError(f"--win-rate {pair!r}: {raw!r} is not a number.") from exc
        if not 0.0 <= rate <= 1.0:
            raise CommandError(f"--win-rate {pair!r}: a rate must be between 0 and 1.")
        rates[model] = rate
    return rates or {}


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4
